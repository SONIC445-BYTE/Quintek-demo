"""
Getting a learner's file onto disk so ingestion can read it.

The picker produced a real `File` and there was nowhere to send it: `sources`
carries a `storage_key`, `IngestionEngine._extract` resolves it under
`storage_dir`, and nothing in the API ever wrote a byte. A PDF could be chosen
and never read.

Base64 inside the JSON body rather than multipart. The whole API is one JSON
shape, `http.server` has no multipart parser, and writing one is a larger
attack surface than the problem deserves. The cost is a 33% inflation on the
wire, which the cap below accounts for.

Two rules the caps encode:

  A REQUEST IS READ INTO MEMORY BEFORE IT IS PARSED. Without a ceiling, one
  request declaring a 4 GB body is the whole server. The limit is checked
  against Content-Length first, so an oversized upload is refused before a
  single byte is read.

  A FILENAME FROM A CLIENT IS NOT A PATH. `../../etc/passwd` is a valid
  filename. The stored name is generated here and the client's name is kept
  only as a label.
"""

from __future__ import annotations

import base64
import binascii
import re
from pathlib import Path

# 20 MB of actual file. A scanned chapter runs to a few MB; a question bank
# PDF can reach fifteen. Beyond that the answer is to split it, not to raise
# the ceiling and hope.
MAX_BYTES = 20 * 1024 * 1024

# What that is once base64'd, plus room for the surrounding JSON. This is the
# figure the transport checks Content-Length against.
MAX_ENCODED_BYTES = (MAX_BYTES * 4 + 2) // 3 + 4096

# Kinds whose bytes are worth storing. `text` arrives as text and needs no
# file; `link` and `video` carry a URL.
BINARY_KINDS = {"pdf", "image"}

_SAFE_SUFFIX = re.compile(r"^[A-Za-z0-9]{1,8}$")


class UploadError(ValueError):
    """The upload cannot be accepted, and the message says why."""


def _suffix_for(filename: str) -> str:
    """
    The extension, if it is plausibly one. Cosmetic: nothing dispatches on it.

    Extraction branches on the source KIND recorded in the database, never on
    the name, so a `.pdf` on a JPEG cannot route it into the PDF reader.
    """
    tail = (filename or "").rsplit(".", 1)
    if len(tail) != 2 or not _SAFE_SUFFIX.match(tail[1]):
        return ""
    return "." + tail[1].lower()


def decode(content_base64: str) -> bytes:
    if not content_base64:
        raise UploadError("no file content was supplied")
    if len(content_base64) > MAX_ENCODED_BYTES:
        raise UploadError(
            f"this file is larger than the {MAX_BYTES // (1024 * 1024)} MB limit")
    # A data: URL is what a browser's FileReader produces if the prefix is not
    # stripped. Accept it rather than failing on something the client is very
    # likely to send.
    if content_base64.startswith("data:"):
        _, _, content_base64 = content_base64.partition(",")
    try:
        raw = base64.b64decode(content_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise UploadError(f"the file content is not valid base64: {exc}") from None
    if not raw:
        raise UploadError("the file is empty")
    if len(raw) > MAX_BYTES:
        raise UploadError(
            f"this file is larger than the {MAX_BYTES // (1024 * 1024)} MB limit")
    return raw


def store(storage_dir: str | Path, source_id: str, filename: str,
          content_base64: str) -> tuple[str, int]:
    """
    Write the bytes and return `(storage_key, size)`.

    The key is derived from the SOURCE ID, which the server generated, so two
    learners uploading `notes.pdf` cannot collide and a crafted name cannot
    escape the directory.
    """
    raw = decode(content_base64)
    directory = Path(storage_dir)
    directory.mkdir(parents=True, exist_ok=True)

    key = f"{source_id}{_suffix_for(filename)}"
    target = directory / key
    # Belt and braces: even with a generated key, refuse anything that did not
    # land where it was meant to.
    if target.parent.resolve() != directory.resolve():
        raise UploadError("refusing to write outside the storage directory")
    target.write_bytes(raw)
    return key, len(raw)
