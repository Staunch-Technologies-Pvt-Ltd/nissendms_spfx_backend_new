"""Document text extraction.

For PDFs we first try the embedded text layer (fast, exact); only if that
yields nothing do we rasterise pages and run PaddleOCR. For images we OCR
directly. Word/Excel go through `office.py`.

PaddleOCR is heavy and downloads models on first use, so it is imported and
instantiated lazily and cached.
"""
from __future__ import annotations

import io

from ..config import settings

_ocr = None


def _get_ocr():
    global _ocr
    if _ocr is None:
        from paddleocr import PaddleOCR

        _ocr = PaddleOCR(use_angle_cls=True, lang="en", show_log=False)
    return _ocr


def _ocr_image_array(arr) -> str:
    result = _get_ocr().ocr(arr, cls=True)
    lines: list[str] = []
    for page in result or []:
        for entry in page or []:
            try:
                text, conf = entry[1]
            except (TypeError, ValueError, IndexError):
                continue
            if conf >= settings.ocr_min_confidence:
                lines.append(text)
    return "\n".join(lines)


def _png_to_array(png_bytes: bytes):
    import numpy as np
    from PIL import Image

    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    return np.array(img)[:, :, ::-1]  # RGB -> BGR for PaddleOCR/OpenCV


def ocr_image_bytes(image_bytes: bytes) -> str:
    return _ocr_image_array(_png_to_array(image_bytes))


def pdf_text(pdf_bytes: bytes, ocr_fallback: bool = True) -> str:
    """Embedded text if present; otherwise OCR each rasterised page."""
    import fitz  # PyMuPDF

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    embedded = "\n".join(page.get_text() for page in doc).strip()
    if embedded:
        return embedded
    if not ocr_fallback:
        return ""
    chunks: list[str] = []
    for page in doc:
        pix = page.get_pixmap(dpi=200)
        chunks.append(_ocr_image_array(_png_to_array(pix.tobytes("png"))))
    return "\n".join(chunks)


def extract_text(file_bytes: bytes, filename: str, content_type: str = "") -> str:
    name = (filename or "").lower()
    if name.endswith(".pdf") or content_type == "application/pdf":
        return pdf_text(file_bytes)
    if name.endswith((".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp")) or (
        content_type.startswith("image/")
    ):
        return ocr_image_bytes(file_bytes)
    if name.endswith(".docx"):
        from .office import docx_text

        return docx_text(file_bytes)
    if name.endswith(".xlsx"):
        from .office import xlsx_text

        return xlsx_text(file_bytes)
    # Legacy .doc/.xls and other unsupported formats: no extraction, which
    # correctly routes the item to manual review rather than a guess.
    if name.endswith((".doc", ".xls", ".ppt", ".pptx", ".msg")):
        return ""
    # Fallback: try decoding as text.
    try:
        return file_bytes.decode("utf-8", errors="ignore")
    except Exception:
        return ""
