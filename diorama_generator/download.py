"""Cached HTTP downloads (resumable) and zip extraction."""

from __future__ import annotations

import hashlib
import time
import zipfile
from pathlib import Path
from typing import Callable, Optional

import httpx

from .config import cache_dir

# Optional observer for byte-level progress: called as hook(filename,
# bytes_done, bytes_total) per chunk (total may be 0 when the server sends no
# content-length). Set by the terminal UI; None means silent, as before.
_progress_hook: Optional[Callable[[str, int, int], None]] = None


def set_progress_hook(hook: Optional[Callable[[str, int, int], None]]) -> None:
    global _progress_hook
    _progress_hook = hook


def _cache_path(url: str) -> Path:
    name = url.rsplit("/", 1)[-1]
    digest = hashlib.sha1(url.encode()).hexdigest()[:10]
    return cache_dir() / "downloads" / f"{digest}_{name}"


def _fetch(client: httpx.Client, url: str, tmp: Path) -> None:
    """Stream `url` into `tmp`, resuming from its current size via HTTP Range.

    Raises httpx.RemoteProtocolError on a short body, so callers can retry and
    pick up where the connection dropped (multi-GB swisstopo downloads get cut
    off routinely).
    """
    pos = tmp.stat().st_size if tmp.exists() else 0
    headers = {"Range": f"bytes={pos}-"} if pos else {}
    name = url.rsplit("/", 1)[-1]
    with client.stream("GET", url, headers=headers) as r:
        if pos and r.status_code == 416:  # already have the full body
            return
        if pos and r.status_code != 206:  # server ignored the range
            pos = 0
        r.raise_for_status()
        total = pos + int(r.headers.get("content-length") or 0)
        done = pos
        if _progress_hook:
            _progress_hook(name, done, total)
        with open(tmp, "ab" if pos else "wb") as fh:
            for chunk in r.iter_bytes(1 << 20):
                fh.write(chunk)
                done += len(chunk)
                if _progress_hook:
                    _progress_hook(name, done, total)


def download(url: str, *, client: httpx.Client | None = None,
             retries: int = 12) -> Path:
    """Download `url` to the cache (skipped if already present). Returns path.

    Interrupted transfers leave a ``.part`` file and are resumed — both across
    retries here and across separate invocations.
    """
    dest = _cache_path(url)
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    owns = client is None
    client = client or httpx.Client(timeout=120.0, follow_redirects=True)
    try:
        for attempt in range(retries):
            try:
                _fetch(client, url, tmp)
                break
            except httpx.TransportError:
                if attempt == retries - 1:
                    raise
                time.sleep(min(2 ** attempt, 30))
    finally:
        if owns:
            client.close()
    tmp.replace(dest)
    return dest


def download_and_unzip(url: str, *, client: httpx.Client | None = None) -> Path:
    """Download a zip and extract it to a sibling cache dir. Returns extract dir."""
    zpath = download(url, client=client)
    out = zpath.with_suffix(".extracted")
    marker = out / ".done"
    if marker.exists():
        return out
    out.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zpath) as z:
        z.extractall(out)
    marker.write_text("ok")
    return out
