"""
`storage_key` came from the request body, and that was two disclosure routes.

WHAT IT WAS
-----------
`StudentAPI.add_source` read `storage_key` from the body and stored it
verbatim. `IngestionEngine._extract` resolved it as
`self.storage_dir / row["storage_key"]` and read whatever was there.

    absolute path   under pathlib an absolute right-hand operand REPLACES the
                    base, so `storage_dir / "/etc/x"` is `/etc/x`.
    ../             walks out of the directory.
    another         a well-formed path INSIDE the storage directory that
    learner's key   belongs to someone else.

Supplying it also skipped `uploads.store` entirely -- no size cap, no base64
validation, no checksum -- because that branch runs only when `content` is
present.

THE TEST THAT MATTERS MOST IS `TestContainmentAloneIsNotEnough`
----------------------------------------------------------------
The obvious reading of this bug is "validate the path". That reading is wrong,
and expensive to arrive at twice, so it is disproved here by execution rather
than argued: the class below reintroduces the body field WITH the containment
check left in place, and the cross-tenant read still succeeds -- because one
learner's storage key is not outside the storage directory. It is right next
to their own.

The fix is that the field is not read from the request at all. Containment is
depth, and depth only.
"""

from __future__ import annotations

import base64
import pathlib
import tempfile

import pytest

from student.api import StudentAPI
from student.db import Database
from student.ingestion import ExtractionUnavailable, IngestionEngine

SECRET = "CONFIDENTIAL-ALPHA-PATIENT-9917"


def one_page_pdf(text: str) -> bytes:
    """A minimal readable PDF, built here so the test needs no PDF writer."""
    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R"
        b" /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n").encode()
    return bytes(out)


@pytest.fixture
def world(tmp_path):
    db = Database(tmp_path / "q.db")
    engine = IngestionEngine(db, storage_dir=tmp_path / "storage")
    api = StudentAPI(db, engine=engine)

    def register(email):
        return api.handle("POST", "/auth/register", {},
                          {"email": email, "password": "correct-horse"}, None)[1]["token"]

    def notebook(token, title):
        return api.handle("POST", "/notebooks", {},
                          {"title": title, "subject": "Medicine"}, token)[1]["id"]

    a, b = register("a@example.com"), register("b@example.com")
    world = type("World", (), {})()
    world.db, world.api, world.engine, world.tmp = db, api, engine, tmp_path
    world.a, world.b = a, b
    world.na, world.nb = notebook(a, "A private"), notebook(b, "B own")

    status, out = api.handle(
        "POST", f"/notebooks/{world.na}/sources", {},
        {"kind": "pdf", "filename": "a.pdf",
         "content_base64": base64.b64encode(one_page_pdf(SECRET)).decode()}, a)
    assert status < 300, out
    world.a_source = out["source_id"]
    world.a_key = db.query_one("SELECT storage_key FROM sources WHERE id = ?",
                               (world.a_source,))["storage_key"]
    return world


def chunks_owned_by(db, notebook_id):
    return [r["text"] or "" for r in db.query(
        "SELECT ch.text FROM source_chunks ch"
        "  JOIN sources s ON s.id = ch.source_id"
        " WHERE s.notebook_id = ?", (notebook_id,))]


class TestTheFieldIsNotReadFromTheRequest:

    def test_a_supplied_storage_key_does_not_reach_the_database(self, world):
        status, out = world.api.handle(
            "POST", f"/notebooks/{world.nb}/sources", {},
            {"kind": "pdf", "filename": "mine.pdf", "storage_key": world.a_key,
             "content_base64": base64.b64encode(one_page_pdf("B's own file")).decode()},
            world.b)
        assert status < 300, out
        stored = world.db.query_one("SELECT storage_key FROM sources WHERE id = ?",
                                    (out["source_id"],))["storage_key"]
        assert stored != world.a_key, (
            "the client's storage_key was stored. It must be derived from the source "
            "id the server generated, never taken from the request.")
        assert stored.startswith(out["source_id"])

    @pytest.mark.parametrize("key", [
        "/etc/passwd",
        "../SECRET_OUTSIDE_STORAGE.txt",
        "../../etc/passwd",
        "src_someone_elses_id.pdf",
    ])
    def test_a_binary_source_with_no_bytes_is_refused_at_the_door(self, world, key):
        """
        The `and not body.get("storage_key")` escape hatch is gone: a pdf or
        image source with no `content_base64` is refused, whatever else the
        body claims.
        """
        status, out = world.api.handle(
            "POST", f"/notebooks/{world.nb}/sources", {},
            {"kind": "pdf", "filename": "x.pdf", "storage_key": key}, world.b)
        assert status == 400, f"expected a refusal, got {status}: {out}"
        assert "needs the file itself" in str(out)

    def test_b_cannot_read_as_content_what_a_uploaded(self, world):
        """
        The original exploit, end to end, asserted on CONTENT. A status code is
        not evidence of confidentiality.

        NO `content_base64` is sent, and that detail is the whole test. With
        bytes attached, `uploads.store` overwrites `storage_key` with the
        server-generated one and the client's value never wins -- so a version
        of this test that sent content would pass even with the defect fully
        restored. It is the bytes-free request that carried the leak.
        """
        status, out = world.api.handle(
            "POST", f"/notebooks/{world.nb}/sources", {},
            {"kind": "pdf", "filename": "mine.pdf", "storage_key": world.a_key},
            world.b)
        if status < 300:                      # only reachable if the defect is back
            world.engine.process_source(out["source_id"])
        for text in chunks_owned_by(world.db, world.nb):
            assert SECRET not in text, (
                "A's document reached B's chunks. The bytes-free request is the one "
                "that carried this, because it is the only path where the client's "
                "storage_key survives to the database.")

    def test_the_upload_path_still_works(self, world):
        """The fix must not have closed the route by breaking it."""
        status, out = world.api.handle(
            "POST", f"/notebooks/{world.nb}/sources", {},
            {"kind": "pdf", "filename": "b.pdf",
             "content_base64": base64.b64encode(one_page_pdf("B-OWN-MATERIAL")).decode()},
            world.b)
        assert status < 300, out
        world.engine.process_source(out["source_id"])
        assert any("B-OWN-MATERIAL" in t for t in chunks_owned_by(world.db, world.nb))


class TestContainmentIsDepthOnly:
    """`_extract` refuses a path outside storage_dir even if one gets there."""

    @pytest.mark.parametrize("key, label", [
        ("/etc/passwd", "absolute"),
        ("../outside.pdf", "relative traversal"),
    ])
    def test_a_key_outside_the_storage_directory_is_refused(self, world, key, label):
        # Written straight to the row: the API no longer accepts it, so this
        # tests the second line of defence on its own.
        #
        # `process_source` CATCHES ExtractionUnavailable and records it against
        # the source rather than raising -- one bad source must not stop a
        # queue -- so the contract to assert is the recorded failure, not a
        # traceback. Asserting `pytest.raises` here passed nothing and proved
        # nothing; it failed honestly instead, which is how this was noticed.
        world.db.execute("UPDATE sources SET storage_key = ? WHERE id = ?",
                         (key, world.a_source))
        world.engine.process_source(world.a_source)

        row = world.db.query_one("SELECT status, error FROM sources WHERE id = ?",
                                 (world.a_source,))
        assert "outside the storage directory" in (row["error"] or ""), (
            f"a {label} key was not refused; the source recorded: {row['error']!r}")
        assert row["status"] == "failed"
        assert chunks_owned_by(world.db, world.na) == [], (
            "nothing may be chunked from a path outside the storage directory")

    def test_a_legitimate_key_is_still_read(self, world):
        world.engine.process_source(world.a_source)
        assert any(SECRET in t for t in chunks_owned_by(world.db, world.na))


class TestContainmentAloneIsNotEnough:
    """
    The point of this file.

    Reintroduce the body field, leave the containment check in place, and watch
    the cross-tenant read succeed anyway -- because another learner's storage
    key is INSIDE the storage directory, which is exactly where a containment
    check permits.

    If this test ever fails, the containment check has started catching the
    cross-tenant case and the reasoning above should be revisited. It should
    not be "fixed" by weakening it.
    """

    def test_containment_permits_one_learner_to_name_anothers_key(self, world):
        assert world.engine._within_storage(world.engine.storage_dir / world.a_key), (
            "A's key is inside the storage directory -- so containment cannot "
            "distinguish it from B's own, and cannot be the fix.")

    def test_the_leak_returns_if_the_body_field_does(self, world, monkeypatch):
        """
        Simulates exactly the mutation a future change might make: accept the
        field again, keep the containment check. The document still crosses.
        """
        original = StudentAPI.add_source

        def accepts_storage_key(self, uid, nid, body):
            result = original(self, uid, nid, body)
            if body.get("storage_key"):          # the reintroduced defect
                self.db.execute("UPDATE sources SET storage_key = ? WHERE id = ?",
                                (body["storage_key"], result["source_id"]))
            return result

        monkeypatch.setattr(StudentAPI, "add_source", accepts_storage_key)
        status, out = world.api.handle(
            "POST", f"/notebooks/{world.nb}/sources", {},
            {"kind": "pdf", "filename": "mine.pdf", "storage_key": world.a_key,
             "content_base64": base64.b64encode(one_page_pdf("B's own file")).decode()},
            world.b)
        assert status < 300, out
        world.engine.process_source(out["source_id"])

        leaked = [t for t in chunks_owned_by(world.db, world.nb) if SECRET in t]
        assert leaked, (
            "the containment check caught the cross-tenant read. That is not what it "
            "does -- A's key is inside storage_dir. If this now passes, work out what "
            "actually changed before trusting it.")


class TestStorageIsOwnerOnly:
    """
    Uploaded documents are other people's material. No route serves them --
    `_extract` is the only reader -- so the exposure is anything else running
    as another user on the same host, which a default 0o755 grants.
    """

    def test_the_directory_and_the_file_are_owner_only(self, world, tmp_path):
        import stat
        from student.uploads import store

        storage = tmp_path / "fresh"
        key, _size, _digest = store(storage, "src_abc", "n.pdf",
                                    base64.b64encode(one_page_pdf("x")).decode())
        assert stat.S_IMODE(storage.stat().st_mode) == 0o700
        assert stat.S_IMODE((storage / key).stat().st_mode) == 0o600

    def test_an_existing_loose_directory_is_tightened(self, tmp_path):
        """
        `mkdir(exist_ok=True)` says nothing about the mode of a directory that
        already existed, so the mode is applied on every store rather than only
        at creation.
        """
        import stat
        from student.uploads import store

        storage = tmp_path / "loose"
        storage.mkdir(mode=0o755)
        store(storage, "src_abc", "n.pdf",
              base64.b64encode(one_page_pdf("x")).decode())
        assert stat.S_IMODE(storage.stat().st_mode) == 0o700

    def test_a_filesystem_that_refuses_chmod_does_not_fail_the_upload(
            self, tmp_path, monkeypatch):
        """
        Losing a learner's upload to a chmod that the filesystem does not
        support would be trading something real for a threat that may not exist
        on that host.
        """
        from pathlib import Path
        from student.uploads import store

        def refuse(self, mode):
            raise OSError("chmod not supported here")

        monkeypatch.setattr(Path, "chmod", refuse)
        key, size, _ = store(tmp_path / "nochmod", "src_abc", "n.pdf",
                             base64.b64encode(one_page_pdf("x")).decode())
        assert size > 0 and key == "src_abc.pdf"

    def test_no_route_serves_a_raw_upload(self):
        """
        The reason storage permissions are the whole of this control: nothing
        hands a stored file back over HTTP. If that changes, this fails and the
        new route needs an ownership check before it ships.
        """
        import pathlib as _p
        api = _p.Path("student/api.py").read_text()
        for giveaway in ("application/pdf", "octet-stream", "read_bytes()",
                         "send_file", "Content-Disposition"):
            assert giveaway not in api, (
                f"student/api.py now contains {giveaway!r}. If a route serves stored "
                "files, it needs an ownership join -- storage_key is derived from a "
                "source id, and knowing an id must not be enough to read the file.")
