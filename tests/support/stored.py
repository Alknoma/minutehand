"""Every byte the store keeps on disk, as a search for a secret must see it.

The world file, its write-ahead log and anything beside them, raw; and then every stored body decompressed, since a secret that reached a compressed body would not appear in the raw bytes and
the search would pass for the wrong reason.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import zstandard

from minutehand.adapters.store.sqlite import Codec


def everything(directory: Path) -> bytes:
    """Every file under `directory`, raw, then each world file's stored bodies and pooled files decompressed."""
    unpack = zstandard.ZstdDecompressor()
    found = sorted(p for p in directory.rglob("*") if p.is_file())
    kept = [p.read_bytes() for p in found]
    for world in (p for p in found if p.suffix == ".db"):
        db = sqlite3.connect(f"{world.resolve().as_uri()}?mode=ro", uri=True)
        try:
            for codec, stored in db.execute("SELECT codec, stored FROM content"):
                kept.append(unpack.decompress(stored) if Codec(codec) is Codec.ZSTD else stored)
        finally:
            db.close()
    return b"".join(kept)
