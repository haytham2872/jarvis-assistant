"""Files sent from the phone (slice 7): photos, screenshots and documents, held until the message
they belong to is sent.

The phone uploads each file on its own (POST /uploads, the raw bytes as the body), gets an id
back, and then sends its message naming those ids (POST /messages, "uploads"). The message route
hands the files to attachments.receive, the same reader Discord uses, and they are deleted from
here. A file never claimed by a message is swept after UPLOAD_TTL_S.

Kept on disk rather than in memory so a large PDF does not sit in the process, and so an upload
survives the few seconds between the two requests even across a reconnect. Not a store of record:
the vault's attachments/ folder is, once the message is sent.
"""
from __future__ import annotations

import json
import logging
import re
import secrets
import time
import zipfile
import io
from dataclasses import dataclass
from pathlib import Path

from backend.brain import attachments

log = logging.getLogger(__name__)

_DIR = Path(__file__).resolve().parents[2] / ".jarvis_uploads"
UPLOAD_TTL_S = 3600.0
MAX_UPLOAD_BYTES = attachments.MAX_DOCUMENT_BYTES
MAX_PER_MESSAGE = attachments.MAX_FILES
_ID = re.compile(r"^up-[0-9a-f]{24}$")

# A file named only by its type (a Shortcut's share sheet often sends no name).
_EXT_FOR_TYPE = {
    "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png", "image/webp": ".webp",
    "image/gif": ".gif", "image/heic": ".heic", "image/heif": ".heic", "application/pdf": ".pdf",
    "text/plain": ".txt", "text/csv": ".csv", "text/markdown": ".md", "application/json": ".json",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
}
_HEIC = {".heic", ".heif"}


class Refused(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


@dataclass
class Upload:
    upload_id: str
    name: str
    content_type: str
    kind: str
    size: int


def sniff(data: bytes) -> str:
    """The extension a file's first bytes give away, or "": a Shortcut's share sheet can send a
    file with no name and a vague type."""
    head = data[:16]
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head.startswith(b"\x89PNG"):
        return ".png"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    if head.startswith(b"%PDF"):
        return ".pdf"
    if head[4:8] == b"ftyp" and head[8:12] in (b"heic", b"heix", b"mif1", b"msf1", b"heis", b"hevc"):
        return ".heic"
    if head.startswith(b"PK\x03\x04"):
        try:
            names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        except Exception:
            return ""
        for prefix, ext in (("word/", ".docx"), ("xl/", ".xlsx"), ("ppt/", ".pptx")):
            if any(n.startswith(prefix) for n in names):
                return ext
    return ""


def file_name(name: str, content_type: str, data: bytes = b"") -> str:
    """A safe file name with an extension the reader understands: the one given, else the
    type's, else what the bytes show."""
    base = Path(str(name or "").replace("\\", "/")).name.strip()[:120]
    ct = (content_type or "").split(";")[0].strip().lower()
    if not Path(base).suffix:
        ext = _EXT_FOR_TYPE.get(ct) or sniff(data)
        if ext:
            base = (base or "shared") + ext
    return base or "file"


def check(name: str, content_type: str, size: int) -> str:
    """The file's kind, or Refused: what the phone can tell him before the message is sent."""
    if size <= 0:
        raise Refused(400, "the file is empty")
    if size > MAX_UPLOAD_BYTES:
        raise Refused(413, "the file is over 25 MB")
    if Path(name).suffix.lower() in _HEIC and not attachments.HEIC_OK:
        raise Refused(415, "HEIC photos can't be read here: send it as JPEG")
    kind = attachments.classify(name, content_type)
    if kind == "audio":
        raise Refused(415, "send a voice message with the microphone instead")
    if kind == "unsupported":
        raise Refused(415, f"I can't read {Path(name).suffix or 'this kind of'} files")
    if kind == "image" and size > attachments.MAX_IMAGE_BYTES:
        raise Refused(413, "the image is over 20 MB")
    return kind


def _sweep(now: float) -> None:
    if not _DIR.exists():
        return
    for meta in _DIR.glob("*.json"):
        try:
            if now - meta.stat().st_mtime > UPLOAD_TTL_S:
                meta.with_suffix(".bin").unlink(missing_ok=True)
                meta.unlink(missing_ok=True)
        except OSError:
            pass


def put(name: str, content_type: str, data: bytes, now: float | None = None) -> Upload:
    """Keep one file; Refused when it can't be read anyway."""
    now = time.time() if now is None else now
    name = file_name(name, content_type, data)
    kind = check(name, content_type, len(data))
    _sweep(now)
    _DIR.mkdir(parents=True, exist_ok=True)
    upload_id = "up-" + secrets.token_hex(12)
    (_DIR / f"{upload_id}.bin").write_bytes(data)
    (_DIR / f"{upload_id}.json").write_text(json.dumps(
        {"name": name, "content_type": content_type, "kind": kind, "size": len(data), "at": now}),
        encoding="utf-8")
    return Upload(upload_id, name, content_type, kind, len(data))


def take(upload_ids: list, now: float | None = None) -> list[tuple[str, str, bytes]]:
    """The files for one message as attachments.receive wants them. Refused (410) if any is
    unknown or expired: the message is not sent half-attached."""
    now = time.time() if now is None else now
    ids = [str(u) for u in upload_ids]
    if len(ids) > MAX_PER_MESSAGE:
        raise Refused(413, f"at most {MAX_PER_MESSAGE} files a message")
    if len(set(ids)) != len(ids):
        raise Refused(400, "the same file twice")
    out = []
    for uid in ids:
        meta_path, data_path = _DIR / f"{uid}.json", _DIR / f"{uid}.bin"
        if not _ID.match(uid) or not meta_path.exists() or not data_path.exists():
            raise Refused(410, "a file was not found on the server (sent too long ago?): attach it again")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if now - float(meta.get("at", 0)) > UPLOAD_TTL_S:
            raise Refused(410, "a file was attached too long ago: attach it again")
        out.append((meta["name"], meta.get("content_type", ""), data_path.read_bytes()))
    return out


def drop(upload_ids: list) -> None:
    """Forget files once their message has them (the vault keeps its own copy)."""
    for uid in map(str, upload_ids):
        if _ID.match(uid):
            (_DIR / f"{uid}.bin").unlink(missing_ok=True)
            (_DIR / f"{uid}.json").unlink(missing_ok=True)
