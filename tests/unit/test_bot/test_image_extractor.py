"""Tests for image validation and Telegram delivery helpers."""

from pathlib import Path
from unittest.mock import patch

import pytest

from src.bot.utils.image_extractor import (
    IMAGE_EXTENSIONS,
    MAX_FILE_SIZE_BYTES,
    PHOTO_SIZE_LIMIT,
    TELEGRAM_PHOTO_EXTENSIONS,
    ImageAttachment,
    ImageIdentityError,
    open_validated,
    should_send_as_photo,
    validate_image_path,
)


@pytest.fixture
def work_dir(tmp_path: Path) -> Path:
    """Create a working directory with some image files."""
    img_dir = tmp_path / "project"
    img_dir.mkdir()
    for name in [
        "chart.png",
        "photo.jpg",
        "diagram.svg",
        "anim.gif",
        "pic.webp",
        "old.bmp",
        "shot.jpeg",
    ]:
        (img_dir / name).write_bytes(b"\x00" * 100)
    return img_dir


@pytest.fixture
def approved_dir(tmp_path: Path) -> Path:
    """The approved directory is tmp_path itself."""
    return tmp_path


# --- should_send_as_photo ---


class TestShouldSendAsPhoto:
    def test_raster_small_as_photo(self, tmp_path: Path):
        img = tmp_path / "small.png"
        img.write_bytes(b"\x00" * 100)
        assert should_send_as_photo(img) is True

    def test_svg_as_document(self, tmp_path: Path):
        img = tmp_path / "diagram.svg"
        img.write_bytes(b"<svg></svg>")
        assert should_send_as_photo(img) is False

    def test_large_raster_as_document(self, tmp_path: Path):
        img = tmp_path / "big.png"
        img.write_bytes(b"\x00" * 100)
        with patch.object(Path, "stat") as mock_stat:
            mock_stat.return_value.st_size = PHOTO_SIZE_LIMIT + 1
            assert should_send_as_photo(img) is False

    def test_nonexistent_file(self, tmp_path: Path):
        img = tmp_path / "gone.png"
        assert should_send_as_photo(img) is False


# --- Constants ---


class TestConstants:
    def test_telegram_photo_extensions_subset(self):
        """TELEGRAM_PHOTO_EXTENSIONS should be a subset of IMAGE_EXTENSIONS keys."""
        for ext in TELEGRAM_PHOTO_EXTENSIONS:
            assert ext in IMAGE_EXTENSIONS

    def test_svg_not_in_photo_extensions(self):
        assert ".svg" not in TELEGRAM_PHOTO_EXTENSIONS


# --- validate_image_path (MCP tool validation) ---


class TestValidateImagePath:
    def test_valid_absolute_image(self, work_dir: Path, approved_dir: Path):
        img = work_dir / "chart.png"
        result = validate_image_path(str(img), approved_dir)
        assert result is not None
        assert result.path == img.resolve()
        assert result.mime_type == "image/png"

    def test_relative_path_rejected(self, approved_dir: Path):
        result = validate_image_path("relative/chart.png", approved_dir)
        assert result is None

    def test_nonexistent_file_rejected(self, work_dir: Path, approved_dir: Path):
        result = validate_image_path(str(work_dir / "missing.png"), approved_dir)
        assert result is None

    def test_non_image_extension_rejected(self, work_dir: Path, approved_dir: Path):
        txt = work_dir / "notes.txt"
        txt.write_text("hello")
        result = validate_image_path(str(txt), approved_dir)
        assert result is None

    def test_outside_approved_dir_rejected(self, tmp_path: Path):
        outside = tmp_path / "outside"
        outside.mkdir()
        img = outside / "evil.png"
        img.write_bytes(b"\x00" * 100)
        # approved is a subdirectory, image is outside it
        approved = tmp_path / "approved"
        approved.mkdir()
        result = validate_image_path(str(img), approved)
        assert result is None

    def test_caption_stored_as_original_reference(
        self, work_dir: Path, approved_dir: Path
    ):
        img = work_dir / "chart.png"
        result = validate_image_path(str(img), approved_dir, caption="My chart")
        assert result is not None
        assert result.original_reference == "My chart"

    def test_no_caption_uses_path(self, work_dir: Path, approved_dir: Path):
        img = work_dir / "chart.png"
        result = validate_image_path(str(img), approved_dir)
        assert result is not None
        assert result.original_reference == str(img)

    def test_large_file_rejected(self, work_dir: Path, approved_dir: Path):
        big = work_dir / "huge.png"
        big.write_bytes(b"\x00" * 100)
        with patch.object(Path, "stat") as mock_stat:
            mock_stat.return_value.st_size = MAX_FILE_SIZE_BYTES + 1
            with patch.object(Path, "is_file", return_value=True):
                result = validate_image_path(str(big), approved_dir)
        assert result is None

    def test_symlink_escaping_rejected(self, tmp_path: Path):
        approved = tmp_path / "approved"
        approved.mkdir()
        outside = tmp_path / "secret"
        outside.mkdir()
        secret_img = outside / "secret.png"
        secret_img.write_bytes(b"\x00" * 100)
        link = approved / "link.png"
        # Symlink creation needs a privilege the Windows test runner may lack
        # (WinError 1314); skip rather than fail in that environment.
        try:
            link.symlink_to(secret_img)
        except OSError as exc:
            pytest.skip(f"Cannot create symlinks in this environment: {exc}")
        result = validate_image_path(str(link), approved)
        assert result is None

    def test_all_supported_extensions(self, work_dir: Path, approved_dir: Path):
        """Every extension in IMAGE_EXTENSIONS should be accepted."""
        for ext in IMAGE_EXTENSIONS:
            fname = f"test_file{ext}"
            (work_dir / fname).write_bytes(b"\x00" * 10)
            result = validate_image_path(str(work_dir / fname), approved_dir)
            assert result is not None, f"Failed for {ext}"
            assert result.mime_type == IMAGE_EXTENSIONS[ext]

    def test_case_insensitive_extension(self, work_dir: Path, approved_dir: Path):
        """Extensions like .PNG or .JPG should still match."""
        upper = work_dir / "UPPER.PNG"
        upper.write_bytes(b"\x00" * 100)
        result = validate_image_path(str(upper), approved_dir)
        assert result is not None

    def test_image_attachment_fields(self, work_dir: Path, approved_dir: Path):
        img = work_dir / "chart.png"
        result = validate_image_path(str(img), approved_dir)
        assert result is not None
        assert isinstance(result, ImageAttachment)
        assert result.mime_type == "image/png"
        assert result.original_reference == str(img)

    def test_records_file_identity(self, work_dir: Path, approved_dir: Path):
        """Identity is recorded so delivery can prove the file didn't change."""
        img = work_dir / "chart.png"
        result = validate_image_path(str(img), approved_dir)
        assert result is not None
        stat_result = img.stat()
        assert result.st_dev == stat_result.st_dev
        assert result.st_ino == stat_result.st_ino
        assert result.size == stat_result.st_size
        assert result.approved_directory == approved_dir.resolve()


# --- open_validated (TOCTOU protection between validation and delivery) ---


class TestOpenValidated:
    def test_reads_validated_bytes(self, work_dir: Path, approved_dir: Path):
        img = work_dir / "chart.png"
        img.write_bytes(b"\x01" * 64)
        attachment = validate_image_path(str(img), approved_dir)
        assert attachment is not None

        with open_validated(attachment) as fh:
            assert fh.read() == b"\x01" * 64

    def test_rejects_content_swapped_after_validation(
        self, work_dir: Path, approved_dir: Path
    ):
        """A file replaced between validation and send must not be delivered."""
        img = work_dir / "chart.png"
        img.write_bytes(b"\x01" * 64)
        attachment = validate_image_path(str(img), approved_dir)
        assert attachment is not None

        # Same path, different contents -> different size (and inode on POSIX).
        img.unlink()
        img.write_bytes(b"\x02" * 128)

        with pytest.raises(ImageIdentityError):
            open_validated(attachment)

    def test_rejects_symlink_swapped_outside_approved_dir(self, tmp_path: Path):
        """The swap this guards against: path -> symlink escaping the root."""
        approved = tmp_path / "approved"
        approved.mkdir()
        img = approved / "chart.png"
        img.write_bytes(b"\x00" * 100)

        attachment = validate_image_path(str(img), approved)
        assert attachment is not None

        outside = tmp_path / "secret"
        outside.mkdir()
        secret = outside / "secret.png"
        secret.write_bytes(b"\x00" * 100)

        img.unlink()
        try:
            img.symlink_to(secret)
        except OSError as exc:
            pytest.skip(f"Cannot create symlinks in this environment: {exc}")

        with pytest.raises(ImageIdentityError):
            open_validated(attachment)

    def test_missing_file_raises_oserror(self, work_dir: Path, approved_dir: Path):
        img = work_dir / "chart.png"
        attachment = validate_image_path(str(img), approved_dir)
        assert attachment is not None

        img.unlink()
        with pytest.raises(OSError):
            open_validated(attachment)

    def test_attachment_without_identity_only_checks_boundary(self, tmp_path: Path):
        """Attachments built outside validate_image_path still open."""
        img = tmp_path / "chart.png"
        img.write_bytes(b"\x03" * 32)
        attachment = ImageAttachment(
            path=img,
            mime_type="image/png",
            original_reference=str(img),
        )

        with open_validated(attachment) as fh:
            assert fh.read() == b"\x03" * 32
