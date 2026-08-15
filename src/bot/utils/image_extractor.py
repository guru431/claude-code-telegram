"""Validate image file paths and prepare them for Telegram delivery.

Used by the MCP ``send_image_to_user`` tool intercept — the stream callback
validates each path via :func:`validate_image_path` and collects
:class:`ImageAttachment` objects for later Telegram delivery.
"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Optional

import structlog

logger = structlog.get_logger()

# Supported image extensions -> MIME types
IMAGE_EXTENSIONS = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
}

# Raster formats that can be sent via reply_photo() (Telegram supports these natively)
TELEGRAM_PHOTO_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}

# Animated formats that must be sent via reply_animation() to preserve motion
# (reply_photo would deliver only a single static frame).
TELEGRAM_ANIMATION_EXTENSIONS = {".gif"}

# Safety caps
MAX_IMAGES_PER_RESPONSE = 10
MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB
PHOTO_SIZE_LIMIT = 10 * 1024 * 1024  # 10 MB — Telegram photo API limit


@dataclass
class ImageAttachment:
    """An image file to attach to a Telegram response.

    ``st_dev``/``st_ino``/``size`` record the identity of the exact file that
    passed validation, so :func:`open_validated` can prove at send time that
    the bytes it is about to read are the bytes that were checked. They default
    to ``None`` for attachments built outside :func:`validate_image_path`, in
    which case :func:`open_validated` only re-checks the directory boundary.
    """

    path: Path
    mime_type: str
    original_reference: str
    st_dev: Optional[int] = None
    st_ino: Optional[int] = None
    size: Optional[int] = None
    approved_directory: Optional[Path] = None


def validate_image_path(
    file_path: str,
    approved_directory: Path,
    caption: str = "",
) -> Optional[ImageAttachment]:
    """Validate a single image path from an MCP ``send_image_to_user`` call.

    Returns an :class:`ImageAttachment` if the path is a valid, existing image
    inside *approved_directory*, or ``None`` otherwise.
    """
    try:
        path = Path(file_path)
        if not path.is_absolute():
            return None

        resolved = path.resolve()

        # Security: must be within approved directory
        try:
            resolved.relative_to(approved_directory.resolve())
        except ValueError:
            logger.debug(
                "MCP image path outside approved directory",
                path=str(resolved),
                approved=str(approved_directory),
            )
            return None

        if not resolved.is_file():
            return None

        stat_result = resolved.stat()
        file_size = stat_result.st_size
        if file_size > MAX_FILE_SIZE_BYTES:
            logger.debug("MCP image file too large", path=str(resolved), size=file_size)
            return None

        ext = resolved.suffix.lower()
        mime_type = IMAGE_EXTENSIONS.get(ext)
        if not mime_type:
            return None

        return ImageAttachment(
            path=resolved,
            mime_type=mime_type,
            original_reference=caption or file_path,
            st_dev=stat_result.st_dev,
            st_ino=stat_result.st_ino,
            size=file_size,
            approved_directory=approved_directory.resolve(),
        )
    except (OSError, ValueError) as e:
        logger.debug("MCP image path validation failed", path=file_path, error=str(e))
        return None


class ImageIdentityError(Exception):
    """The file at send time is not the file that passed validation."""


def open_validated(attachment: ImageAttachment) -> BinaryIO:
    """Open *attachment* for reading, proving it is still the validated file.

    :func:`validate_image_path` runs long before delivery, so between the two
    the path can be swapped for a symlink pointing outside the approved
    directory — Claude can write inside that directory, so the agent that
    proposed the path can also perform the swap.

    This closes the window by opening the file first and then ``fstat``-ing the
    *open handle*: the identity that is checked belongs to the very object the
    caller will read from, so no further swap can affect it. The re-resolved
    path is also re-checked against the recorded approved directory.

    Known limit: identity is ``(st_dev, st_ino, st_size)``, i.e. metadata. A
    rewrite in place that keeps the inode *and* the exact byte count passes.
    That is deliberate — the check exists to stop the file being swapped for one
    outside the approved directory, not to freeze its contents, and hashing
    every image on delivery would cost a full extra read.

    Raises:
        ImageIdentityError: if the boundary or the recorded identity no longer
            holds.
        OSError: if the file cannot be opened.
    """
    resolved = attachment.path.resolve()

    if attachment.approved_directory is not None:
        try:
            resolved.relative_to(attachment.approved_directory)
        except ValueError:
            raise ImageIdentityError(
                f"image path now resolves outside the approved directory: "
                f"{resolved}"
            ) from None

    handle: BinaryIO = open(resolved, "rb")
    try:
        current = os.fstat(handle.fileno())
        expected = (attachment.st_dev, attachment.st_ino, attachment.size)
        if expected != (None, None, None) and expected != (
            current.st_dev,
            current.st_ino,
            current.st_size,
        ):
            raise ImageIdentityError(
                f"image file changed between validation and send: {resolved}"
            )
    except BaseException:
        handle.close()
        raise
    return handle


def should_send_as_photo(path: Path) -> bool:
    """Return True if the image should be sent via reply_photo().

    Raster images ≤ 10 MB are sent as photos (inline preview).
    SVGs, animations (.gif) and large files are sent another way.
    """
    ext = path.suffix.lower()
    if ext not in TELEGRAM_PHOTO_EXTENSIONS:
        return False

    try:
        return path.stat().st_size <= PHOTO_SIZE_LIMIT
    except OSError:
        return False


def should_send_as_animation(path: Path) -> bool:
    """Return True if the image should be sent via reply_animation().

    Animated formats (.gif) ≤ 10 MB are sent as animations so motion is
    preserved; reply_photo would deliver only a single static frame.
    """
    ext = path.suffix.lower()
    if ext not in TELEGRAM_ANIMATION_EXTENSIONS:
        return False

    try:
        return path.stat().st_size <= PHOTO_SIZE_LIMIT
    except OSError:
        return False
