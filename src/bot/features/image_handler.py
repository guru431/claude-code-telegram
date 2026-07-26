"""
Handle image uploads for UI/screenshot analysis

The analysis prompt is chosen from the caption the user sent with the photo
(see :meth:`ImageHandler._detect_image_type`): "here is the architecture
diagram" gets a diagram prompt, "review this mockup" gets a UI prompt, and
anything else falls back to the screenshot prompt. Nothing is inferred from the
pixels — an image-content classifier would be guesswork we cannot verify.
"""

import base64
from dataclasses import dataclass
from typing import Any, Dict, Optional

from telegram import PhotoSize

from src.bot.utils.upload_limits import exceeds_upload_limit
from src.config import Settings

# Caption keywords selecting a non-default analysis prompt. Checked in order,
# so an ambiguous caption resolves deterministically.
_CAPTION_TYPE_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("diagram", ("diagram", "chart", "flowchart", "schema", "architecture", "uml")),
    ("ui_mockup", ("mockup", "wireframe", "ui design", "figma", "layout")),
    ("screenshot", ("screenshot", "screen shot", "screen capture")),
)


@dataclass
class ProcessedImage:
    """Processed image result"""

    prompt: str
    image_type: str
    base64_data: str
    size: int
    metadata: Optional[Dict[str, Any]] = None


class ImageHandler:
    """Process image uploads"""

    def __init__(self, config: Settings):
        self.config = config
        self.supported_formats = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

    async def process_image(
        self, photo: PhotoSize, caption: Optional[str] = None
    ) -> ProcessedImage:
        """Process uploaded image"""

        # Reject an oversized photo from its metadata *before* pulling the bytes
        # into memory. Telegram's file_size is optional and understatable, so
        # this is only a cheap first pass — validate_image re-checks the real
        # byte length after the download.
        max_bytes = self.config.max_file_upload_size_bytes
        if exceeds_upload_limit(getattr(photo, "file_size", None), max_bytes):
            raise ValueError(
                f"Image too large (max {self.config.max_file_upload_size_mb}MB)"
            )

        # Download image
        file = await photo.get_file()
        if exceeds_upload_limit(getattr(file, "file_size", None), max_bytes):
            raise ValueError(
                f"Image too large (max {self.config.max_file_upload_size_mb}MB)"
            )
        image_bytes = await file.download_as_bytearray()

        # Validate size and format before processing
        is_valid, error = await self.validate_image(bytes(image_bytes))
        if not is_valid:
            raise ValueError(error or "Invalid image")

        # Detect image type
        image_type = self._detect_image_type(caption)

        # Create appropriate prompt
        if image_type == "screenshot":
            prompt = self._create_screenshot_prompt(caption)
        elif image_type == "diagram":
            prompt = self._create_diagram_prompt(caption)
        elif image_type == "ui_mockup":
            prompt = self._create_ui_prompt(caption)
        else:
            prompt = self._create_generic_prompt(caption)

        base64_image = base64.b64encode(image_bytes).decode("utf-8")

        return ProcessedImage(
            prompt=prompt,
            image_type=image_type,
            base64_data=base64_image,
            size=len(image_bytes),
            metadata={
                "format": self._detect_format(image_bytes),
                "has_caption": caption is not None,
            },
        )

    def _detect_image_type(self, caption: Optional[str]) -> str:
        """Pick the analysis prompt from what the user said about the image.

        The caption is the only signal we can actually check; classifying the
        pixels would need a model we do not run here. Without a matching
        keyword the screenshot prompt is used — screenshots are by far the most
        common upload and its questions fit an arbitrary image well enough.
        """
        text = (caption or "").casefold()
        for image_type, keywords in _CAPTION_TYPE_KEYWORDS:
            if any(keyword in text for keyword in keywords):
                return image_type
        return "screenshot"

    def _detect_format(self, image_bytes: bytes) -> str:
        """Detect image format from magic bytes"""
        # Check magic bytes for common formats
        if image_bytes.startswith(b"\x89PNG"):
            return "png"
        elif image_bytes.startswith(b"\xff\xd8\xff"):
            return "jpeg"
        elif image_bytes.startswith(b"GIF87a") or image_bytes.startswith(b"GIF89a"):
            return "gif"
        elif image_bytes.startswith(b"RIFF") and b"WEBP" in image_bytes[:12]:
            return "webp"
        else:
            return "unknown"

    def _create_screenshot_prompt(self, caption: Optional[str]) -> str:
        """Create prompt for screenshot analysis"""
        base_prompt = """I'm sharing a screenshot with you. Please analyze it and help me with:

1. Identifying what application or website this is from
2. Understanding the UI elements and their purpose
3. Any issues or improvements you notice
4. Answering any specific questions I have

"""
        if caption:
            base_prompt += f"Specific request: {caption}"

        return base_prompt

    def _create_diagram_prompt(self, caption: Optional[str]) -> str:
        """Create prompt for diagram analysis"""
        base_prompt = """I'm sharing a diagram with you. Please help me:

1. Understand the components and their relationships
2. Identify the type of diagram (flowchart, architecture, etc.)
3. Explain any technical concepts shown
4. Suggest improvements or clarifications

"""
        if caption:
            base_prompt += f"Specific request: {caption}"

        return base_prompt

    def _create_ui_prompt(self, caption: Optional[str]) -> str:
        """Create prompt for UI mockup analysis"""
        base_prompt = """I'm sharing a UI mockup with you. Please analyze:

1. The layout and visual hierarchy
2. User experience considerations
3. Accessibility aspects
4. Implementation suggestions
5. Any potential improvements

"""
        if caption:
            base_prompt += f"Specific request: {caption}"

        return base_prompt

    def _create_generic_prompt(self, caption: Optional[str]) -> str:
        """Create generic image analysis prompt"""
        base_prompt = """I'm sharing an image with you. Please analyze it and provide relevant insights.

"""
        if caption:
            base_prompt += f"Context: {caption}"

        return base_prompt

    def supports_format(self, filename: str) -> bool:
        """Check if image format is supported"""
        if not filename:
            return False

        # Extract extension
        parts = filename.lower().split(".")
        if len(parts) < 2:
            return False

        extension = f".{parts[-1]}"
        return extension in self.supported_formats

    async def validate_image(self, image_bytes: bytes) -> tuple[bool, Optional[str]]:
        """Validate image data"""
        # Check the real byte count against the one configured upload limit
        # (MAX_FILE_UPLOAD_SIZE_MB), not a second hard-coded 10MB rule.
        max_size = self.config.max_file_upload_size_bytes
        if len(image_bytes) > max_size:
            return (
                False,
                f"Image too large (max {self.config.max_file_upload_size_mb}MB)",
            )

        # Check format
        format_type = self._detect_format(image_bytes)
        if format_type == "unknown":
            return False, "Unsupported image format"

        # Basic validity check
        if len(image_bytes) < 100:  # Too small to be a real image
            return False, "Invalid image data"

        return True, None
