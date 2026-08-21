"""Bounded streaming of Starlette uploads to a private temporary file."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import Tuple


async def spool_upload(upload, directory: str, max_bytes: int,
                       chunk_size: int = 1024 * 1024) -> Tuple[str, int, str]:
    if max_bytes <= 0 or chunk_size <= 0:
        raise ValueError("Upload limits must be positive")
    Path(directory).mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="wb", prefix="upload-", suffix=".part", dir=directory, delete=False
    )
    path = handle.name
    size = 0
    digest = hashlib.sha256()
    try:
        while True:
            chunk = await upload.read(chunk_size)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise ValueError(
                    f"Uploaded file exceeds the {max_bytes}-byte import limit"
                )
            digest.update(chunk)
            handle.write(chunk)
        handle.flush()
        os.fsync(handle.fileno())
        if size == 0:
            raise ValueError("Uploaded file is empty")
        return path, size, digest.hexdigest()
    except BaseException:
        handle.close()
        Path(path).unlink(missing_ok=True)
        raise
    finally:
        if not handle.closed:
            handle.close()
