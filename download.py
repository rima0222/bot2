"""Resumable HTTP downloads (HTTP Range) used for datasets / model weights.

If the connection drops, the next attempt continues from the bytes already on
disk (``<dest>.part``). The finished file is verified (size + optional sha256)
and moved into place atomically.
"""
from __future__ import annotations

import hashlib
import logging
import os
import sys
import time
from pathlib import Path
from typing import Callable

import requests

log = logging.getLogger("bot.download")

CHUNK = 256 * 1024


class DownloadError(RuntimeError):
    pass


def _progress_bar(total: int | None, initial: int, desc: str):
    try:
        from tqdm import tqdm  # optional nicety

        if sys.stderr.isatty():
            return tqdm(total=total, initial=initial, unit="B", unit_scale=True, desc=desc)
    except Exception:  # noqa: BLE001
        pass
    return None


def fetch_resumable(
    url: str,
    dest: str | Path,
    *,
    sha256: str | None = None,
    retries: int = 10,
    timeout: float = 30.0,
    backoff: float = 2.0,
    max_backoff: float = 60.0,
    session: requests.Session | None = None,
    sleep: Callable[[float], None] = time.sleep,
    headers: dict[str, str] | None = None,
) -> Path:
    """Download ``url`` to ``dest``, resuming from ``dest.part`` after failures."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    sess = session or requests.Session()
    attempt = 0

    while True:
        have = part.stat().st_size if part.exists() else 0
        hdrs = dict(headers or {})
        hdrs["Accept-Encoding"] = "identity"  # byte offsets must match the file
        if have:
            hdrs["Range"] = f"bytes={have}-"
        try:
            with sess.get(url, headers=hdrs, stream=True, timeout=timeout) as r:
                if r.status_code == 416:  # already have everything we asked for
                    total = have
                elif r.status_code == 206:
                    total = _total_from_content_range(r.headers.get("Content-Range")) or None
                elif r.status_code == 200:
                    if have:  # server ignored Range -> start over
                        log.warning("server ignored Range for %s; restarting download", url)
                        part.unlink(missing_ok=True)
                        have = 0
                    total = int(r.headers.get("Content-Length", 0)) or None
                else:
                    r.raise_for_status()
                    raise DownloadError(f"unexpected HTTP {r.status_code}")

                if r.status_code != 416:
                    bar = _progress_bar(total, have, dest.name)
                    with open(part, "ab") as fh:
                        for chunk in r.iter_content(CHUNK):
                            if chunk:
                                fh.write(chunk)
                                if bar:
                                    bar.update(len(chunk))
                    if bar:
                        bar.close()

            size = part.stat().st_size
            if total and size != total:
                raise requests.ConnectionError(f"incomplete download: {size}/{total} bytes")
            if sha256:
                digest = _sha256(part)
                if digest.lower() != sha256.lower():
                    part.unlink(missing_ok=True)  # corrupt -> restart from zero
                    raise DownloadError(f"sha256 mismatch for {url}")
            os.replace(part, dest)
            return dest
        except (requests.RequestException, OSError, DownloadError) as exc:
            attempt += 1
            if isinstance(exc, DownloadError) and "sha256" in str(exc) and attempt >= 2:
                raise
            if attempt > retries:
                raise DownloadError(f"giving up on {url} after {retries} retries: {exc}") from exc
            delay = min(max_backoff, backoff * (2 ** (attempt - 1)))
            log.warning("download interrupted (%s); retry %d/%d in %.0fs, resuming at byte %d",
                        exc, attempt, retries, delay, part.stat().st_size if part.exists() else 0)
            sleep(delay)


def _total_from_content_range(value: str | None) -> int | None:
    # "bytes 100-999/1000"
    if not value or "/" not in value:
        return None
    tail = value.rsplit("/", 1)[1].strip()
    return int(tail) if tail.isdigit() else None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


if __name__ == "__main__":  # python download.py URL DEST [sha256]
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) < 3:
        sys.exit("usage: python download.py URL DEST [sha256]")
    fetch_resumable(sys.argv[1], sys.argv[2], sha256=sys.argv[3] if len(sys.argv) > 3 else None)
