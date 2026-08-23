from __future__ import annotations

import os
import re
import uuid
from pathlib import Path
from typing import Any

from fastapi import UploadFile
from PIL import Image, UnidentifiedImageError

from config import UPLOAD_ROOT

BASE_DIR = Path(__file__).resolve().parent
COVER_DIR = UPLOAD_ROOT / "covers"
AUDIO_DIR = UPLOAD_ROOT / "audio"

COVER_MAX_BYTES = 20 * 1024 * 1024
AUDIO_MAX_BYTES = 500 * 1024 * 1024
COVER_MIN_SIDE = 1400
COVER_MAX_SIDE = 6000
COVER_MIN_DPI = 72

ALLOWED_COVER_EXTENSIONS = {".jpg", ".jpeg", ".png"}
ALLOWED_AUDIO_EXTENSIONS = {".wav", ".flac"}

TIME_PATTERN = re.compile(r"^(?:(\d{1,2}):)?([0-5]?\d)$")


class UploadValidationError(ValueError):
    pass


def ensure_upload_directories() -> None:
    COVER_DIR.mkdir(parents=True, exist_ok=True)
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)


def _safe_original_name(filename: str | None, fallback: str) -> str:
    raw = Path(filename or fallback).name.strip()
    clean = re.sub(r"[^\w.()\- ]+", "_", raw, flags=re.UNICODE).strip(" ._")
    return clean[:180] or fallback


def _random_relative_path(folder: str, extension: str) -> str:
    return f"{folder}/{uuid.uuid4().hex}{extension.lower()}"


def resolve_upload(relative_path: str | None) -> Path | None:
    if not relative_path:
        return None
    candidate = (UPLOAD_ROOT / relative_path).resolve()
    root = UPLOAD_ROOT.resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return candidate


def delete_upload(relative_path: str | None) -> None:
    path = resolve_upload(relative_path)
    if not path:
        return
    try:
        path.unlink()
    except OSError:
        pass


async def _stream_to_file(upload: UploadFile, target: Path, max_bytes: int) -> tuple[int, bytes]:
    total = 0
    header = b""
    try:
        with target.open("wb") as destination:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                if not header:
                    header = chunk[:32]
                total += len(chunk)
                if total > max_bytes:
                    raise UploadValidationError("Файл слишком большой.")
                destination.write(chunk)
    except Exception:
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    finally:
        await upload.close()
    return total, header


def _is_jpeg(header: bytes) -> bool:
    return header.startswith(b"\xff\xd8\xff")


def _is_png(header: bytes) -> bool:
    return header.startswith(b"\x89PNG\r\n\x1a\n")


def _is_wav(header: bytes) -> bool:
    return len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WAVE"


def _is_flac(header: bytes) -> bool:
    return header.startswith(b"fLaC")


async def save_cover_upload(upload: UploadFile | None) -> dict[str, Any] | None:
    if not upload or not upload.filename:
        return None

    ensure_upload_directories()
    extension = Path(upload.filename).suffix.lower()
    if extension not in ALLOWED_COVER_EXTENSIONS:
        raise UploadValidationError("Обложка должна быть в формате JPG или PNG.")

    relative_path = _random_relative_path("covers", extension)
    target = UPLOAD_ROOT / relative_path
    size, header = await _stream_to_file(upload, target, COVER_MAX_BYTES)

    valid_signature = {
        ".jpg": _is_jpeg,
        ".jpeg": _is_jpeg,
        ".png": _is_png,
    }[extension](header)
    if not valid_signature:
        target.unlink(missing_ok=True)
        raise UploadValidationError("Не удалось распознать изображение. Выберите другую обложку.")

    try:
        with Image.open(target) as image:
            image.verify()
        with Image.open(target) as image:
            width, height = image.size
            image_format = (image.format or "").upper()
            dpi_value = image.info.get("dpi")
    except (UnidentifiedImageError, OSError, ValueError):
        target.unlink(missing_ok=True)
        raise UploadValidationError("Файл обложки повреждён или имеет неподдерживаемый формат.")

    if width != height:
        target.unlink(missing_ok=True)
        raise UploadValidationError("Обложка должна быть квадратной.")
    if width < COVER_MIN_SIDE or height < COVER_MIN_SIDE:
        target.unlink(missing_ok=True)
        raise UploadValidationError(
            f"Минимальный размер обложки — {COVER_MIN_SIDE}×{COVER_MIN_SIDE} пикселей."
        )
    if width > COVER_MAX_SIDE or height > COVER_MAX_SIDE:
        target.unlink(missing_ok=True)
        raise UploadValidationError(
            f"Максимальный размер обложки — {COVER_MAX_SIDE}×{COVER_MAX_SIDE} пикселей."
        )
    numeric_dpi: list[float] = []
    if isinstance(dpi_value, (int, float)):
        numeric_dpi = [float(dpi_value)]
    elif isinstance(dpi_value, (tuple, list)) and dpi_value:
        numeric_dpi = [float(value) for value in dpi_value[:2] if isinstance(value, (int, float))]
    if numeric_dpi:
        if min(numeric_dpi) < COVER_MIN_DPI:
            target.unlink(missing_ok=True)
            raise UploadValidationError(
                f"Разрешение обложки должно быть не менее {COVER_MIN_DPI} dpi."
            )

    return {
        "path": relative_path,
        "filename": _safe_original_name(upload.filename, f"cover{extension}"),
        "size": size,
        "width": width,
        "height": height,
        "format": image_format,
    }


async def save_audio_upload(upload: UploadFile | None) -> dict[str, Any] | None:
    if not upload or not upload.filename:
        return None

    ensure_upload_directories()
    extension = Path(upload.filename).suffix.lower()
    if extension not in ALLOWED_AUDIO_EXTENSIONS:
        raise UploadValidationError("Аудиофайл должен быть в формате WAV или FLAC.")

    relative_path = _random_relative_path("audio", extension)
    target = UPLOAD_ROOT / relative_path
    size, header = await _stream_to_file(upload, target, AUDIO_MAX_BYTES)

    valid = _is_wav(header) if extension == ".wav" else _is_flac(header)
    if not valid:
        target.unlink(missing_ok=True)
        raise UploadValidationError("Не удалось распознать аудиофайл. Выберите WAV или FLAC.")
    if size < 256:
        target.unlink(missing_ok=True)
        raise UploadValidationError("Аудиофайл пустой или повреждён.")

    return {
        "path": relative_path,
        "filename": _safe_original_name(upload.filename, f"track{extension}"),
        "size": size,
        "extension": extension.lstrip("."),
    }


def parse_timecode(value: str, *, field_label: str) -> int:
    clean = value.strip()
    if not clean:
        return 0
    if clean.isdigit():
        seconds = int(clean)
    else:
        match = TIME_PATTERN.fullmatch(clean)
        if not match:
            raise ValueError(f"{field_label}: используйте формат ММ:СС.")
        minutes = int(match.group(1) or 0)
        seconds = minutes * 60 + int(match.group(2))
    if seconds < 0 or seconds > 600:
        raise ValueError(f"{field_label}: допустимое значение — от 00:00 до 10:00.")
    return seconds


def format_timecode(seconds: int | None) -> str:
    value = max(0, int(seconds or 0))
    return f"{value // 60:02d}:{value % 60:02d}"


def format_file_size(size_bytes: int | None) -> str:
    size = int(size_bytes or 0)
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} МБ".replace(".0", "")
    if size >= 1024:
        return f"{size / 1024:.1f} КБ".replace(".0", "")
    return f"{size} Б"
