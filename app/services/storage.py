import os
import uuid
from abc import ABC, abstractmethod
from pathlib import Path

from fastapi import HTTPException

from ..core.config import settings


def detect_image_type(data: bytes) -> str:
    """Inspects file magic bytes to determine image format.

    Returns the file extension (e.g. '.jpg', '.png', '.webp', '.gif')
    or raises HTTPException(400) if not a supported image format.
    """
    if len(data) >= 3 and data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if len(data) >= 8 and data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if len(data) >= 6 and (data.startswith(b"GIF87a") or data.startswith(b"GIF89a")):
        return ".gif"
    raise HTTPException(400, "Invalid image file. Supported formats: JPEG, PNG, WebP, GIF")


class StorageBackend(ABC):
    @abstractmethod
    def save(self, content: bytes, extension: str) -> str:
        """Saves image content and returns the public URL."""
        ...

    @abstractmethod
    def delete(self, url: str) -> None:
        """Deletes the stored image if it exists."""
        ...


class LocalStorage(StorageBackend):
    def __init__(self, media_dir: str | None = None):
        self.media_dir = Path(media_dir or settings.media_dir)
        self.media_dir.mkdir(parents=True, exist_ok=True)

    def save(self, content: bytes, extension: str) -> str:
        # Never use original filename — generate secure random UUID filename
        filename = f"{uuid.uuid4().hex}{extension}"
        file_path = self.media_dir / filename
        file_path.write_bytes(content)
        return f"/media/{filename}"

    def delete(self, url: str) -> None:
        if not url:
            return
        prefix = "/media/"
        if url.startswith(prefix):
            filename = url[len(prefix):]
            file_path = self.media_dir / filename
            if file_path.exists() and file_path.is_file():
                try:
                    file_path.unlink()
                except OSError:
                    pass


# Default storage instance (pluggable with S3Storage or CloudinaryStorage in production)
storage: StorageBackend = LocalStorage()
