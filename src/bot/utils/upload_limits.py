"""Shared document upload size policy.

Telegram's ``file_size`` is optional metadata and is attacker-influenced: it may
be absent, or understated relative to the bytes actually served. Treating an
absent size as ``0`` silently bypasses the limit, so callers must instead treat
an unknown size as "not yet verified" and re-check ``len(downloaded_bytes)``
after the download completes.

The limit itself comes from ``Settings.max_file_upload_size_bytes``
(``MAX_FILE_UPLOAD_SIZE_MB``) so every upload path enforces one policy.
"""

from typing import Optional


def exceeds_upload_limit(file_size: Optional[int], max_bytes: int) -> bool:
    """Return True only when a *known* size is over the limit.

    An unknown (``None``) size is not a pass — it is undetermined, and the
    caller is responsible for re-checking the real byte length after download.
    """
    return isinstance(file_size, int) and file_size > max_bytes


def format_file_size(size: float) -> str:
    """Format a byte count for display.

    Single implementation for every listing (``/ls``, the LS button, the file
    handler's tree): three near-identical copies used to disagree on whether
    plain bytes carry a decimal, so the same directory rendered as "0B" in one
    view and "0.0B" in another.
    """
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024.0:
            return f"{int(size)}B" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024.0
    return f"{size:.1f}TB"
