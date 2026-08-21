"""
Getting a learner's file onto disk.

The picker produced a real `File` and there was nowhere to send it: `sources`
carried a `storage_key`, `IngestionEngine._extract` resolved it under
`storage_dir`, and no endpoint ever wrote a byte. A PDF could be chosen and
never read. These tests cover the write path and the two things that go wrong
when a server starts accepting files -- size and filenames.
"""

from __future__ import annotations

import base64
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from student.api import StudentAPI
from student.db import Database
from student.server import MAX_REQUEST_BYTES, make_handler
from student.uploads import MAX_BYTES, UploadError, decode, store


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


# ------------------------------------------------------------------ storing

def test_a_file_is_written_where_ingestion_will_look_for_it(tmp_path) -> None:
    key, size = store(tmp_path, "src_1", "iron handling.pdf", b64(b"%PDF-1.4 body"))
    assert (tmp_path / key).read_bytes() == b"%PDF-1.4 body"
    assert size == len(b"%PDF-1.4 body")


def test_the_stored_name_comes_from_the_source_id_not_the_client(tmp_path) -> None:
    """
    `../../etc/passwd` is a valid filename. The key is derived from an id the
    SERVER generated, so a crafted name cannot choose where the bytes land.
    """
    key, _ = store(tmp_path, "src_9", "../../etc/passwd", b64(b"x"))
    assert key.startswith("src_9")
    assert "/" not in key and ".." not in key
    assert (tmp_path / key).exists()


def test_two_learners_uploading_the_same_filename_do_not_collide(tmp_path) -> None:
    first, _ = store(tmp_path, "src_a", "notes.pdf", b64(b"one"))
    second, _ = store(tmp_path, "src_b", "notes.pdf", b64(b"two"))
    assert first != second
    assert (tmp_path / first).read_bytes() == b"one"
    assert (tmp_path / second).read_bytes() == b"two"


def test_an_odd_extension_is_dropped_rather_than_trusted(tmp_path) -> None:
    key, _ = store(tmp_path, "src_c", "notes.p df/../x", b64(b"x"))
    assert key == "src_c"


def test_a_data_url_prefix_is_accepted() -> None:
    """What a browser's FileReader produces if the prefix is not stripped."""
    assert decode("data:application/pdf;base64," + b64(b"hello")) == b"hello"


def test_a_file_over_the_limit_is_refused() -> None:
    with pytest.raises(UploadError) as exc:
        decode(b64(b"x" * (MAX_BYTES + 1)))
    assert "larger than" in str(exc.value)


def test_an_empty_file_is_refused() -> None:
    with pytest.raises(UploadError):
        decode(b64(b""))


def test_content_that_is_not_base64_is_refused_clearly() -> None:
    with pytest.raises(UploadError) as exc:
        decode("this is not base64 !!!")
    assert "base64" in str(exc.value)


# ------------------------------------------------------------------ the API

@pytest.fixture()
def world(tmp_path):
    from student.generation import AIConceptExtractor
    from student.ingestion import IngestionEngine

    db = Database(tmp_path / "student.db")
    engine = IngestionEngine(db, storage_dir=tmp_path / "storage")
    api = StudentAPI(db, engine=engine)

    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(api))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address
    yield {"base": f"http://{host}:{port}", "api": api, "db": db,
           "storage": tmp_path / "storage", "engine": engine}
    engine.stop()
    server.shutdown()
    server.server_close()


def call(base, method, path, body=None, token=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(base + path, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def learner(world, email="up@x.com"):
    status, auth = call(world["base"], "POST", "/auth/register",
                        {"email": email, "password": "correct horse 42", "name": "Up"})
    assert status in (200, 201), auth
    token = auth["token"]
    status, nb = call(world["base"], "POST", "/notebooks",
                      {"title": "Cardio", "subject": "cardiology"}, token=token)
    assert status in (200, 201), nb
    return token, nb["id"]


def test_a_pdf_upload_reaches_the_storage_directory(world) -> None:
    token, notebook = learner(world)
    status, created = call(
        world["base"], "POST", f"/notebooks/{notebook}/sources",
        {"kind": "pdf", "filename": "iron.pdf", "content_base64": b64(b"%PDF-1.4 x")},
        token=token)
    assert status in (200, 201, 202), created
    assert created["bytes_stored"] == len(b"%PDF-1.4 x")

    row = world["db"].query_one("SELECT storage_key, byte_size FROM sources WHERE id=?",
                                (created["source_id"],))
    assert row["storage_key"], "the source was stored with no storage key"
    assert (world["storage"] / row["storage_key"]).exists(), (
        "the file never reached the directory ingestion reads from")


def test_a_pdf_source_with_no_file_is_refused_at_the_door(world) -> None:
    """
    Accepting it creates a row ingestion can only fail on, seconds later, with
    the real cause two tables away.
    """
    token, notebook = learner(world)
    status, payload = call(world["base"], "POST", f"/notebooks/{notebook}/sources",
                           {"kind": "pdf", "filename": "iron.pdf"}, token=token)
    assert status == 400
    assert "needs the file itself" in payload["error"]


def test_an_oversized_upload_is_refused_with_413(world) -> None:
    token, notebook = learner(world)
    status, payload = call(
        world["base"], "POST", f"/notebooks/{notebook}/sources",
        {"kind": "pdf", "filename": "huge.pdf", "content_base64": b64(b"x" * (MAX_BYTES + 10))},
        token=token)
    assert status == 413, payload


def test_a_body_over_the_transport_cap_is_refused_before_it_is_read(world) -> None:
    """
    The body is read into memory whole, so a request DECLARING four gigabytes
    is the whole server. Checked against Content-Length before a byte is read.
    """
    import http.client
    from urllib.parse import urlparse

    token, notebook = learner(world)
    parsed = urlparse(world["base"])

    # http.client at the putheader level, because urllib recomputes
    # Content-Length from the body it is given -- which is precisely the
    # header this test needs to lie about.
    conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=10)
    conn.putrequest("POST", f"/notebooks/{notebook}/sources")
    conn.putheader("Content-Type", "application/json")
    conn.putheader("Authorization", "Bearer " + token)
    conn.putheader("Content-Length", str(MAX_REQUEST_BYTES + 1))
    conn.endheaders()
    # Deliberately send only a token amount. A server that read the declared
    # length first would block here; one that checked it first answers now.
    conn.send(b"{")
    response = conn.getresponse()
    assert response.status == 413, (
        f"declared {MAX_REQUEST_BYTES + 1} bytes and got {response.status}")
    conn.close()


def test_a_text_source_still_needs_no_file(world) -> None:
    token, notebook = learner(world)
    status, created = call(
        world["base"], "POST", f"/notebooks/{notebook}/sources",
        {"kind": "text", "text": "Ferritin is an acute phase reactant that rises "
                                 "in inflammation regardless of iron stores."},
        token=token)
    assert status in (200, 201, 202), created
    assert created["bytes_stored"] == 0


def test_an_uploaded_pdf_is_actually_read(world) -> None:
    """
    The end of the caveat: bytes in, concepts out. Skipped where pypdf is
    absent, because then the deployment genuinely cannot read PDFs and
    `/capabilities` says so.
    """
    pypdf = pytest.importorskip("pypdf")

    import io

    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)

    token, notebook = learner(world)
    status, created = call(
        world["base"], "POST", f"/notebooks/{notebook}/sources",
        {"kind": "pdf", "filename": "blank.pdf", "content_base64": b64(buffer.getvalue())},
        token=token)
    assert status in (200, 201, 202), created

    world["engine"].wait_idle(30)
    status, progress = call(world["base"], "GET",
                            f"/sources/{created['source_id']}/progress", token=token)
    assert status == 200
    # A blank page yields no text, so 'failed' is the CORRECT outcome here --
    # what matters is that the reader ran on real bytes rather than never
    # finding a file.
    assert progress["status"] in {"extracted", "processing", "failed"}
    assert progress["error"] is None or "no file was stored" not in progress["error"], (
        "the PDF reader could not find the uploaded file")


# ------------------------------------------------------------ demonstrations

def _two_learners(api) -> tuple[str, str]:
    """Real rows: question_demos.owner_id is a foreign key into users."""
    ids = []
    for email in ("a@x.com", "b@x.com"):
        status, payload = api.handle(
            "POST", "/auth/register", {},
            {"email": email, "password": "correct horse 42", "name": email}, None)
        assert status in (200, 201), payload
        ids.append(payload["user_id"])
    return ids[0], ids[1]

def test_a_demonstration_belongs_to_the_learner_who_wrote_it(tmp_path) -> None:
    """
    Demonstration ids now travel from the client -- Make Questions sends
    whichever the learner selected. An unscoped lookup would let anyone read
    anyone else's reference question by guessing an id, through a channel that
    puts the text straight into a prompt.
    """
    from student.generation import QuestionGenerator

    db = Database(tmp_path / "s.db")
    api = StudentAPI(db)
    a, b = _two_learners(api)
    mine = api.create_demo(a, {"title": "Mine", "question": "A stem I wrote."})
    theirs = api.create_demo(b, {"title": "Theirs", "question": "A stem THEY wrote."})

    generator = QuestionGenerator(db, ai=None)

    assert [d["id"] for d in generator._demos([mine["id"]], a)] == [mine["id"]]
    assert generator._demos([theirs["id"]], a) == [], (
        "one learner read another learner's reference question")
    # Asking for both returns only your own.
    assert [d["id"] for d in generator._demos([mine["id"], theirs["id"]], a)] \
        == [mine["id"]]


def test_a_demonstrations_text_cannot_reach_another_learners_prompt(tmp_path) -> None:
    from student.generation import QuestionGenerator

    db = Database(tmp_path / "s.db")
    api = StudentAPI(db)
    a, b = _two_learners(api)
    secret = "The patient in MY private reference had a ferritin of 11."
    theirs = api.create_demo(b, {"title": "Private", "question": secret})

    generator = QuestionGenerator(db, ai=None)
    prompt = generator.build_prompt(
        count=1, passages=[{"text": "A passage.", "locator_json": "{}"}],
        target_names=["Ferritin"], related_names=[],
        demos=generator._demos([theirs["id"]], a),
        family="", difficulty="", reasoning_depth="", constraints="")
    assert secret not in prompt


def test_the_grounding_rule_travels_with_every_demonstration(tmp_path) -> None:
    """
    The design's key distinction: demonstrations supply STYLE, never facts.
    """
    from student.generation import QuestionGenerator

    db = Database(tmp_path / "s.db")
    api = StudentAPI(db)
    a, _ = _two_learners(api)
    demo = api.create_demo(a, {"title": "Vignette",
                               "question": "A 34-year-old presents with..."})

    generator = QuestionGenerator(db, ai=None)
    prompt = generator.build_prompt(
        count=1, passages=[{"text": "A passage.", "locator_json": "{}"}],
        target_names=["Ferritin"], related_names=[],
        demos=generator._demos([demo["id"]], a),
        family="", difficulty="", reasoning_depth="", constraints="")

    assert "DEMONSTRATIONS" in prompt
    assert "STYLE ONLY" in prompt
    assert "Never reuse a clinical fact" in prompt
