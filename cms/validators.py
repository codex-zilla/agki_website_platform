"""Upload validators shared across cms models — see design doc §7.3.

Django's FileExtensionValidator only checks the filename extension, not
the actual file content. That's a deliberate, accepted tradeoff here: this
is a low-traffic internal admin used by trusted staff accounts, not a
public upload endpoint, so content-sniffing/magic-byte validation would be
defense against a threat model that doesn't apply. If public uploads are
ever added, revisit this.
"""

from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator

IMAGE_EXTENSIONS = ["jpg", "jpeg", "png", "webp"]
VIDEO_EXTENSIONS = ["mp4", "webm"]

IMAGE_MAX_SIZE_MB = 5
VIDEO_MAX_SIZE_MB = 50

image_extension_validator = FileExtensionValidator(allowed_extensions=IMAGE_EXTENSIONS)
video_extension_validator = FileExtensionValidator(allowed_extensions=VIDEO_EXTENSIONS)


def validate_image_file_size(file):
    if file.size > IMAGE_MAX_SIZE_MB * 1024 * 1024:
        raise ValidationError(
            f"Image is too large ({file.size / (1024 * 1024):.1f}MB). "
            f"Please compress it to under {IMAGE_MAX_SIZE_MB}MB before uploading."
        )


def validate_video_file_size(file):
    if file.size > VIDEO_MAX_SIZE_MB * 1024 * 1024:
        raise ValidationError(
            f"Video is too large ({file.size / (1024 * 1024):.1f}MB). "
            f"Prefer linking a YouTube/Vimeo URL instead of uploading video files directly "
            f"(shared hosting has limited storage/bandwidth — see design doc §6.2). "
            f"If you must upload, keep it under {VIDEO_MAX_SIZE_MB}MB."
        )
