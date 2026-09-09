"""Advanced Multi-Engine Document OCR & Text Extraction Pipeline.

Engine Hierarchy & Benchmarks:
1. PyMuPDF Digital Layer: Fast, lossless 100% precision extraction for native PDFs.
2. PaddleOCR (Primary Vision AI): Top open-source OCR engine. Ships vision-language
   document parsing, auto angle-classification / deskewing (`use_angle_cls=True`),
   superior table/drawing layout recognition, and robust multilingual & CJK support.
3. EasyOCR / Tesseract (Secondary Fallback): Available when installed for specific scripts.
4. Clean Text Validator: Strips OCR artifacts, normalizes unicode, deduplicates noise,
   and computes an extraction confidence score.
"""
from __future__ import annotations

import io
import logging
import re
from typing import Any

from ..config import settings
from .dates import month_label, parse_month

logger = logging.getLogger("vessel_dms.ocr")

_paddle_ocr = None
_easy_ocr = None


def _get_paddle_ocr():
    """Lazily load and cache PaddleOCR instance with angle classification and layout support."""
    global _paddle_ocr
    if _paddle_ocr is None:
        try:
            # pyrefly: ignore [missing-import]
            from paddleocr import PaddleOCR
            _paddle_ocr = PaddleOCR(use_angle_cls=True, lang="en", show_log=False)
            logger.info("PaddleOCR engine initialized successfully.")
        except Exception as exc:
            logger.warning("PaddleOCR not available or failed to initialize: %s", exc)
            _paddle_ocr = False
    return _paddle_ocr if _paddle_ocr is not False else None


def _get_easy_ocr():
    """Secondary fallback: EasyOCR."""
    global _easy_ocr
    if _easy_ocr is None:
        try:
            # pyrefly: ignore [missing-import]
            import easyocr
            _easy_ocr = easyocr.Reader(["en"], verbose=False)
            logger.info("EasyOCR fallback engine initialized.")
        except Exception:
            _easy_ocr = False
    return _easy_ocr if _easy_ocr is not False else None


def normalize_ocr_text(text: str) -> str:
    """Normalize OCR text:
    - Replace smart quotes, unicode hyphens/dashes, symbols
    - Collapse single-character tracking artifacts for letters (e.g. 'G H A N A' -> 'GHANA', 'I N D E X' -> 'INDEX')
    - Collapse single-character tracking artifacts for isolated digits (e.g. '7 2 1' -> '721')
    - Normalize whitespace while preserving line structure
    """
    if not text:
        return ""

    # 1. Normalize unicode quotation marks, accents, dashes, dots
    t = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    t = t.replace("–", "-").replace("—", "-").replace("·", ".").replace("•", ".")

    # 2. Collapse runs of single letters separated by space
    letter_pattern = re.compile(r'(?<!\S)(?:[A-Za-z]\s+)+[A-Za-z](?!\S)')
    t = letter_pattern.sub(lambda m: re.sub(r'\s+', '', m.group(0)), t)

    # 3. Collapse runs of isolated single digits separated by space
    digit_pattern = re.compile(r'(?<!\S)(?:[0-9]\s+)+[0-9](?!\S)')
    t = digit_pattern.sub(lambda m: re.sub(r'\s+', '', m.group(0)), t)

    # 4. Collapse multiple horizontal spaces/tabs on each line
    lines = [re.sub(r'[ \t]+', ' ', line.strip()) for line in t.splitlines()]
    return "\n".join(lines).strip()


def is_text_usable_for_classification(text: str, min_words: int = 4) -> bool:
    """Returns False if extracted text is mostly noise/fragments and shouldn't be trusted for vessel/category matching."""
    if not text or not text.strip():
        return False
    norm = normalize_ocr_text(text)
    words = re.findall(r'\b[A-Za-z0-9]{3,}\b', norm)
    return len(words) >= min_words


def clean_and_validate_ocr_text(raw_text: str) -> str:
    """Filter out OCR gibberish, non-printable noise, normalize wide tracking, and clean up line layout."""
    if not raw_text:
        return ""

    raw_text = normalize_ocr_text(raw_text)
    lines = raw_text.splitlines()
    clean_lines: list[str] = []
    seen_lines: set[str] = set()

    for line in lines:
        cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", line).strip()
        # Collapse multiple spaces
        cleaned = re.sub(r"\s+", " ", cleaned)
        if not cleaned or len(cleaned) < 2:
            continue

        # Filter out lines that are purely non-alphanumeric noise (e.g. `_--_..__`)
        alpha_count = sum(1 for c in cleaned if c.isalnum())
        if alpha_count == 0 and len(cleaned) > 3:
            continue

        norm_key = cleaned.lower()
        # Avoid excessive duplicate OCR glitch lines
        if norm_key in seen_lines and len(cleaned) < 15:
            continue

        seen_lines.add(norm_key)
        clean_lines.append(cleaned)

    result = "\n".join(clean_lines)
    return normalize_ocr_text(result)


def _ocr_with_paddle(image_array) -> tuple[str, float]:
    """Run PaddleOCR on numpy image array and calculate mean recognition confidence."""
    engine = _get_paddle_ocr()
    if engine is None:
        return "", 0.0

    try:
        result = engine.ocr(image_array, cls=True)
        lines: list[str] = []
        confidences: list[float] = []

        for page in result or []:
            for entry in page or []:
                try:
                    text, conf = entry[1]
                except (TypeError, ValueError, IndexError):
                    continue
                if conf >= getattr(settings, "ocr_min_confidence", 0.35):
                    lines.append(text)
                    confidences.append(float(conf))

        mean_conf = (sum(confidences) / len(confidences)) if confidences else 0.0
        return clean_and_validate_ocr_text("\n".join(lines)), round(mean_conf, 2)
    except Exception as exc:
        logger.warning("PaddleOCR extraction exception: %s", exc)
        return "", 0.0


def _ocr_with_easyocr(image_bytes: bytes) -> tuple[str, float]:
    """Run EasyOCR fallback."""
    reader = _get_easy_ocr()
    if reader is None:
        return "", 0.0

    try:
        results = reader.readtext(image_bytes)
        lines: list[str] = []
        confs: list[float] = []
        for bbox, text, prob in results:
            if prob >= 0.35:
                lines.append(text)
                confs.append(float(prob))
        mean_conf = (sum(confs) / len(confs)) if confs else 0.0
        return clean_and_validate_ocr_text("\n".join(lines)), round(mean_conf, 2)
    except Exception as exc:
        logger.warning("EasyOCR fallback exception: %s", exc)
        return "", 0.0


def _ocr_with_tesseract(png_bytes: bytes) -> tuple[str, float]:
    """Run Tesseract fallback if pytesseract + tesseract binary are present."""
    try:
        import pytesseract
        from PIL import Image
        img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
        text = pytesseract.image_to_string(img)
        return clean_and_validate_ocr_text(text), 0.70
    except Exception:
        return "", 0.0


def _png_to_bgr_array(png_bytes: bytes):
    """Convert PNG image bytes to BGR numpy array for vision processing."""
    try:
        import numpy as np
        from PIL import Image
        img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
        return np.array(img)[:, :, ::-1]
    except Exception:
        return None


def ocr_image_bytes(image_bytes: bytes) -> tuple[str, str, float]:
    """Multi-engine image OCR. Returns (extracted_text, engine_name, confidence)."""
    # 1. Try PaddleOCR (Top accuracy on scanned drawings, technical diagrams, tables)
    arr = _png_to_bgr_array(image_bytes)
    if arr is not None:
        text, conf = _ocr_with_paddle(arr)
        if text and len(text) > 10:
            return text, "paddleocr", conf

    # 2. Try EasyOCR
    text, conf = _ocr_with_easyocr(image_bytes)
    if text and len(text) > 10:
        return text, "easyocr", conf

    # 3. Try Tesseract
    text, conf = _ocr_with_tesseract(image_bytes)
    if text and len(text) > 10:
        return text, "tesseract", conf

    return "", "none", 0.0


def pdf_text_with_metadata(pdf_bytes: bytes, ocr_fallback: bool = True) -> tuple[str, str, float]:
    """Extract PDF text: checks embedded text layer first across all pages; falls back to rasterized vision OCR if garbled/unusable."""
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        embedded_pages = [page.get_text() for page in doc]
        embedded = clean_and_validate_ocr_text("\n".join(embedded_pages))

        # Check if embedded text passes the usability quality gate
        if embedded and is_text_usable_for_classification(embedded, min_words=2):
            return embedded, "pymupdf_embedded", 1.0

        if not ocr_fallback:
            return embedded or "", "pymupdf_embedded", 0.40

        # Embedded text is empty, noisy, or failed the quality gate -> rasterize pages and run vision OCR
        logger.info("Embedded PDF text failed quality gate; falling back to multi-engine vision OCR...")
        ocr_chunks: list[str] = []
        conf_scores: list[float] = []
        engine_used = "none"

        max_pages = min(len(doc), 10)
        for page_idx in range(max_pages):
            page = doc[page_idx]
            pix = page.get_pixmap(dpi=200)
            png_bytes = pix.tobytes("png")

            page_text, engine, conf = ocr_image_bytes(png_bytes)
            if page_text:
                page_text = clean_and_validate_ocr_text(page_text)
                ocr_chunks.append(page_text)
                conf_scores.append(conf)
                engine_used = engine

        combined_text = "\n".join(ocr_chunks)
        if combined_text and is_text_usable_for_classification(combined_text, min_words=2):
            mean_conf = (sum(conf_scores) / len(conf_scores)) if conf_scores else 0.85
            return combined_text, engine_used, round(mean_conf, 2)

        # Still low quality / unusable
        final_text = combined_text or embedded
        return final_text, engine_used if combined_text else "pymupdf_embedded", 0.40
    except Exception as exc:
        logger.warning("PyMuPDF PDF extraction failed: %s", exc)
        return "", "failed", 0.0


def extract_text(file_bytes: bytes, filename: str, content_type: str = "") -> str:
    """Primary text extraction entry point. Returns clean extracted text string."""
    name = (filename or "").lower()

    if name.endswith(".pdf") or content_type == "application/pdf":
        text, _, _ = pdf_text_with_metadata(file_bytes, ocr_fallback=True)
        return text

    if name.endswith((".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp")) or content_type.startswith("image/"):
        text, _, _ = ocr_image_bytes(file_bytes)
        return text

    if name.endswith(".docx"):
        try:
            from .office import docx_text
            return clean_and_validate_ocr_text(docx_text(file_bytes))
        except Exception:
            return ""

    if name.endswith(".xlsx"):
        try:
            from .office import xlsx_text
            return clean_and_validate_ocr_text(xlsx_text(file_bytes))
        except Exception:
            return ""

    if name.endswith((".doc", ".xls", ".ppt", ".pptx", ".msg")):
        return ""

    # Text decode fallback
    try:
        decoded = file_bytes.decode("utf-8", errors="ignore")
        return clean_and_validate_ocr_text(decoded)
    except Exception:
        return ""


def extract_text_with_metadata(file_bytes: bytes, filename: str, content_type: str = "") -> dict[str, Any]:
    """Returns structured extraction results including engine name and confidence."""
    name = (filename or "").lower()

    if name.endswith(".pdf") or content_type == "application/pdf":
        text, engine, conf = pdf_text_with_metadata(file_bytes, ocr_fallback=True)
        return {
            "text": text,
            "engine": engine,
            "confidence": conf,
            "line_count": len(text.splitlines()) if text else 0,
            "char_count": len(text),
        }

    if name.endswith((".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp")) or content_type.startswith("image/"):
        text, engine, conf = ocr_image_bytes(file_bytes)
        return {
            "text": text,
            "engine": engine,
            "confidence": conf,
            "line_count": len(text.splitlines()) if text else 0,
            "char_count": len(text),
        }

    extracted = extract_text(file_bytes, filename, content_type)
    return {
        "text": extracted,
        "engine": "native_parser",
        "confidence": 0.95 if extracted else 0.0,
        "line_count": len(extracted.splitlines()) if extracted else 0,
        "char_count": len(extracted),
    }


def detect_document_month(file_bytes: bytes, filename: str, content_type: str = "") -> dict:
    """Return {year, month, label, text_empty}."""
    text = extract_text(file_bytes, filename, content_type)
    text_empty = not (text and text.strip())
    found = parse_month(text)
    if not found:
        return {"year": None, "month": None, "label": None, "text_empty": text_empty}
    year, month = found
    return {"year": year, "month": month, "label": month_label(year, month), "text_empty": text_empty}
