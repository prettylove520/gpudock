"""Move files between this machine and a runtime via the Jupyter contents API.

* Upload: ``PUT {url}/api/contents/{path}`` with ``format: base64``. One request
  must stay under ~50 MB, so larger files go in chunks (``chunk: 1, 2, ..., -1``).
* Download: ``GET {url}/files/{path}``, streamed, resumable with ``Range``.

Remote paths are absolute paths without the leading slash, e.g.
``content/gpudock/in.mp4`` is ``/content/gpudock/in.mp4`` on the runtime.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import quote

import httpx

from .jupyter import RuntimeConnection

log = logging.getLogger(__name__)
DOWNLOAD_RETRIES = 6
UPLOAD_RETRIES = 4
DEFAULT_CHUNK = 16 * 1024 * 1024  # raw bytes; ~21 MB once base64-encoded

Progress = Callable[[int, int], None]


@dataclass
class TransferStats:
    path: str
    bytes: int
    seconds: float
    chunks: int = 1
    resumed_from: int = 0

    @property
    def mb_per_s(self) -> float:
        return (self.bytes - self.resumed_from) / 1_048_576 / self.seconds if self.seconds > 0 else 0.0

    def to_record(self) -> dict:
        return {
            "path": self.path,
            "bytes": self.bytes,
            "seconds": round(self.seconds, 3),
            "chunks": self.chunks,
            "resumed_from": self.resumed_from,
            "mb_per_s": round(self.mb_per_s, 2),
        }


def _norm(remote: str) -> str:
    return remote.lstrip("/")


def _contents(remote: str) -> str:
    return "api/contents/" + quote(_norm(remote), safe="/")


def ensure_dir(conn: RuntimeConnection, remote_dir: str) -> None:
    """Create ``remote_dir`` and its parents. Each request crosses Google's proxy,
    so skip the existence check (PUT on an existing directory is a no-op) and
    remember what this connection already created."""
    made: set[str] = conn.__dict__.setdefault("_made_dirs", set())
    parts = PurePosixPath(_norm(remote_dir)).parts
    for i in range(1, len(parts) + 1):
        sub = "/".join(parts[:i])
        if sub in made:
            continue
        resp = conn.request("PUT", _contents(sub), json={"type": "directory"})
        if resp.status_code not in (200, 201):
            resp.raise_for_status()
        made.add(sub)


def upload(
    conn: RuntimeConnection,
    local: str | Path,
    remote: str,
    *,
    chunk_size: int = DEFAULT_CHUNK,
    on_progress: Progress | None = None,
) -> TransferStats:
    """Upload ``local`` to ``remote``. Chunk appends aren't idempotent, so a
    dropped connection restarts the file from its first chunk (which truncates)."""
    for attempt in range(UPLOAD_RETRIES):
        try:
            return _upload_once(conn, local, remote, chunk_size=chunk_size, on_progress=on_progress)
        except (httpx.TransportError, ssl.SSLError, ConnectionError) as err:
            if attempt == UPLOAD_RETRIES - 1:
                raise
            log.info("upload %s interrupted (%s); restarting", remote, err)
            time.sleep(min(2 ** attempt, 20))
    raise AssertionError("unreachable")


def _upload_once(
    conn: RuntimeConnection,
    local: str | Path,
    remote: str,
    *,
    chunk_size: int,
    on_progress: Progress | None,
) -> TransferStats:
    local = Path(local)
    total = local.stat().st_size
    remote = _norm(remote)
    parent = str(PurePosixPath(remote).parent)
    if parent not in ("", "."):
        ensure_dir(conn, parent)
    name = PurePosixPath(remote).name
    started = time.monotonic()
    sent = 0
    chunks = 0
    with local.open("rb") as fh:
        if total <= chunk_size:
            data = fh.read()
            _put(conn, remote, name, data, chunk=None)
            sent = len(data)
            chunks = 1
            if on_progress:
                on_progress(sent, total)
        else:
            index = 1
            while True:
                data = fh.read(chunk_size)
                last = sent + len(data) >= total
                _put(conn, remote, name, data, chunk=-1 if last else index)
                sent += len(data)
                chunks += 1
                if on_progress:
                    on_progress(sent, total)
                if last:
                    break
                index += 1
    return TransferStats(path=remote, bytes=total, seconds=time.monotonic() - started, chunks=chunks)


def _put(conn: RuntimeConnection, remote: str, name: str, data: bytes, *, chunk: int | None) -> None:
    model = {
        "type": "file",
        "format": "base64",
        "name": name,
        "path": remote,
        "content": base64.b64encode(data).decode("ascii"),
    }
    if chunk is not None:
        model["chunk"] = chunk
    resp = conn.request("PUT", _contents(remote), json=model, timeout=600)
    if resp.status_code not in (200, 201):
        resp.raise_for_status()


def download(
    conn: RuntimeConnection,
    remote: str,
    local: str | Path,
    *,
    resume: bool = True,
    on_progress: Progress | None = None,
    block: int = 1024 * 1024,
) -> TransferStats:
    """Stream ``/files/{remote}`` to ``local``; resume a partial ``.part`` file.

    Dropped connections (common behind proxies) are retried, resuming where
    the partial file left off.
    """
    local = Path(local)
    local.parent.mkdir(parents=True, exist_ok=True)
    part = local.with_name(local.name + ".part")
    if not resume and part.exists():
        part.unlink()
    url_path = "files/" + quote(_norm(remote), safe="/")
    started = time.monotonic()

    refreshed = False
    for attempt in range(DOWNLOAD_RETRIES):
        offset = part.stat().st_size if part.exists() else 0
        headers = conn.headers()
        if offset:
            headers["Range"] = f"bytes={offset}-"
        try:
            with conn.client.stream("GET", conn.url_for(url_path), headers=headers, timeout=600) as resp:
                if resp.status_code in (401, 403, 404) and not refreshed and conn.refresh_token():
                    refreshed = True
                    continue
                if resp.status_code == 416:  # already complete
                    break
                if resp.status_code not in (200, 206):
                    resp.read()
                    raise httpx.HTTPStatusError(f"下载失败 HTTP {resp.status_code}", request=resp.request, response=resp)
                if resp.status_code == 200 and offset:
                    offset = 0  # server ignored Range; start over
                total = _total_size(resp, offset)
                done = offset
                with part.open("ab" if offset else "wb") as fh:
                    for piece in resp.iter_bytes(block):
                        fh.write(piece)
                        done += len(piece)
                        if on_progress:
                            on_progress(done, total or done)
            break
        except (httpx.TransportError, ssl.SSLError, ConnectionError) as err:
            if attempt == DOWNLOAD_RETRIES - 1:
                raise
            log.info("download %s interrupted (%s); retrying", remote, err)
            time.sleep(min(2 ** attempt, 20))
    part.replace(local)
    return TransferStats(
        path=_norm(remote),
        bytes=local.stat().st_size,
        seconds=time.monotonic() - started,
        resumed_from=offset,
    )


def _total_size(resp: httpx.Response, offset: int) -> int | None:
    content_range = resp.headers.get("content-range")
    if content_range and "/" in content_range:
        tail = content_range.rsplit("/", 1)[1]
        if tail.isdigit():
            return int(tail)
    length = resp.headers.get("content-length")
    return int(length) + (offset if resp.status_code == 206 else 0) if length and length.isdigit() else None


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def remote_sha256_code(remote: str, root: str = "/") -> str:
    """Kernel code that stores the sha256 of a runtime file in ``_gpudock_sha_result``.

    ``root`` is the Jupyter server's root directory ("/" on Colab).
    """
    path = str(PurePosixPath(root) / _norm(remote))
    return (
        "import hashlib as _h\n"
        "def _gpudock_sha(p):\n"
        "    h = _h.sha256()\n"
        "    with open(p, 'rb') as f:\n"
        "        for b in iter(lambda: f.read(1 << 20), b''):\n"
        "            h.update(b)\n"
        "    return h.hexdigest()\n"
        f"_gpudock_sha_result = _gpudock_sha({path!r})"
    )
