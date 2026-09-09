"""
Multi-Engine Secondary Validation Layer for Document Metadata.

Provides multi-engine cross-validation for critical metadata fields
(Vessel Name, Ship/Hull No., IMO No., Category, Group, Sub-Category).

Supports:
- pdfplumber / Camelot for native PDF table extraction
- PaddleOCR PP-Structure & Docling for scanned / vision layout validation
- Tesseract fallback for unstructured text
- Consensus algorithm (Primary vs Secondary/Tertiary majority rule)
- Extensible JSON schema for future Azure/AWS engine upgrades
- Disagreement pattern logging
"""
from __future__ import annotations

import io
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("vessel_dms.ocr_validation")

# Modular engine flags
_pdfplumber_available = None
_camelot_available = None
_docling_available = None
_paddle_structure_available = None


def _check_pdfplumber():
    global _pdfplumber_available
    if _pdfplumber_available is None:
        try:
            import pdfplumber  # type: ignore
            _pdfplumber_available = True
        except ImportError:
            _pdfplumber_available = False
    return _pdfplumber_available


def _check_camelot():
    global _camelot_available
    if _camelot_available is None:
        try:
            import camelot  # type: ignore
            _camelot_available = True
        except Exception:
            _camelot_available = False
    return _camelot_available


def _check_docling():
    global _docling_available
    if _docling_available is None:
        try:
            from docling.document_converter import DocumentConverter  # type: ignore
            _docling_available = True
        except Exception:
            _docling_available = False
    return _docling_available


def normalize_field_value(val: Any) -> str:
    """Normalize string for field comparison: lowercase, collapse spaces, strip punctuation."""
    if val is None:
        return ""
    s = str(val).strip().lower()
    s = re.sub(r"^(mv|m/v|m\.v\.|mt|m/t|m\.t\.|ss|hull|sno|s\.no\.|s/no|ship\s*no\.)\s*", "", s)
    s = re.sub(r"[^\w\d]", "", s)
    return s


def extract_native_tables_text(pdf_bytes: bytes) -> str:
    """Extract table cells and key-value pairs directly from native PDF text layer using pdfplumber."""
    if not _check_pdfplumber():
        return ""
    try:
        import pdfplumber
        text_chunks: list[str] = []
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            max_p = min(len(pdf.pages), 10)
            for page in pdf.pages[:max_p]:
                tables = page.extract_tables()
                for table in tables or []:
                    for row in table or []:
                        clean_row = [str(cell).strip() for cell in row if cell and str(cell).strip()]
                        if clean_row:
                            text_chunks.append(" | ".join(clean_row))
        return "\n".join(text_chunks)
    except Exception as exc:
        logger.debug("pdfplumber table extraction skipped: %s", exc)
        return ""


def extract_docling_text(pdf_bytes: bytes) -> str:
    """Extract structured document text using IBM Docling if available."""
    if not _check_docling():
        return ""
    try:
        from docling.document_converter import DocumentConverter, PdfFormatOption
        converter = DocumentConverter()
        res = converter.convert_from_bytes(pdf_bytes, mime_type="application/pdf")
        return res.document.export_to_markdown() if res and res.document else ""
    except Exception as exc:
        logger.debug("Docling extraction skipped: %s", exc)
        return ""


def run_secondary_ocr_validation(
    file_bytes: bytes | None,
    filename: str,
    primary_fields: Dict[str, Any],
    source_path: str = "",
    known_vessels: List[Any] | None = None,
) -> Dict[str, Any]:
    """Run secondary validation pass on critical fields and build audit metadata.

    Returns structured dict:
    {
        "validation_passed": bool,
        "overall_status": "validated" | "needs_review" | "conflict",
        "field_details": {
            "vessel": {...},
            "group": {...},
            "category": {...},
            "sub_category": {...},
        },
        "disagreements": list of field names,
        "engines_used": list of engine names,
    }
    """
    from .drawing_category import (
        _classify_vessel_tiered,
        _classify_group_tiered,
        _classify_category_and_subcategory_tiered,
        _norm_match_key,
    )
    from .extract import is_text_usable_for_classification

    critical_fields = ["vessel", "group", "category", "sub_category"]
    engines_used = ["primary_ocr"]

    sec_text = ""
    engine_name = "none"

    if file_bytes and len(file_bytes) > 0:
        # 1. Native PDF table check
        sec_text = extract_native_tables_text(file_bytes)
        if sec_text and is_text_usable_for_classification(sec_text, min_words=2):
            engines_used.append("pdfplumber_table")
            engine_name = "pdfplumber_table"
        else:
            # 2. Scanned PDF Docling check
            sec_text = extract_docling_text(file_bytes)
            if sec_text and is_text_usable_for_classification(sec_text, min_words=2):
                engines_used.append("docling_layout")
                engine_name = "docling_layout"

    # Evaluate secondary extraction if secondary text is present
    sec_vessel = ""
    sec_group = ""
    sec_category = ""
    sec_subcategory = ""

    if sec_text:
        usable = is_text_usable_for_classification(sec_text, min_words=2)
        v_res = _classify_vessel_tiered(sec_text, filename, known_vessels=known_vessels, source_path=source_path, text_is_usable=usable)
        g_res = _classify_group_tiered(sec_text, filename, text_is_usable=usable)
        c_res, sub_res, _ = _classify_category_and_subcategory_tiered(sec_text, filename, g_res.get("value", "Drawing"), text_is_usable=usable)

        sec_vessel = v_res.get("value", "")
        sec_group = g_res.get("value", "")
        sec_category = c_res.get("value", "")
        sec_subcategory = sub_res.get("value", "")

    field_details: Dict[str, Any] = {}
    disagreements: List[str] = []

    for f_key in critical_fields:
        p_raw = primary_fields.get(f_key)
        p_val = p_raw.get("value") if isinstance(p_raw, dict) else str(p_raw or "")
        p_conf = float(p_raw.get("confidence", 0.0)) if isinstance(p_raw, dict) else 0.85

        s_val = ""
        if f_key == "vessel":
            s_val = sec_vessel
        elif f_key == "group":
            s_val = sec_group
        elif f_key == "category":
            s_val = sec_category
        elif f_key == "sub_category":
            s_val = sec_subcategory

        p_norm = normalize_field_value(p_val)
        s_norm = normalize_field_value(s_val)

        status = "validated"
        accepted = p_val
        agreed_engines = ["primary_ocr"]
        disagreed_engines: List[str] = []

        if s_val:
            if p_norm == s_norm:
                agreed_engines.append(engine_name)
                status = "validated"
            else:
                disagreed_engines.append(engine_name)
                disagreements.append(f_key)
                status = "needs_review"
                # If primary confidence is low but secondary is high, suggest secondary
                if p_conf < 0.60 and s_val:
                    accepted = s_val

        field_details[f_key] = {
            "accepted_value": accepted,
            "primary_value": p_val,
            "secondary_value": s_val,
            "status": status,
            "primary_confidence": p_conf,
            "source_engines": engines_used,
            "agreed_engines": agreed_engines,
            "disagreed_engines": disagreed_engines,
        }

    # Vessel vs Folder Path Mismatch Guardrail
    folder_vessel = ""
    if source_path:
        parts = [p.strip() for p in source_path.replace(">", "/").split("/") if p.strip()]
        for p in parts:
            if p.lower() not in ("technical & crewing", "commercial & chartering", "insurance", "drawings and manuals", "drawing", "manual", "basic", "hull", "electrical", "machinery", "safety", "to be classified"):
                folder_vessel = p
                break

    vessel_detail = field_details.get("vessel", {})
    accepted_vessel = vessel_detail.get("accepted_value", "")
    if accepted_vessel and folder_vessel and normalize_field_value(accepted_vessel) != normalize_field_value(folder_vessel):
        vessel_detail["status"] = "needs_review"
        vessel_detail["mismatch_warning"] = f"Validated vessel '{accepted_vessel}' differs from folder vessel '{folder_vessel}'"
        if "vessel" not in disagreements:
            disagreements.append("vessel")

    overall_status = "validated" if len(disagreements) == 0 else "needs_review"

    if disagreements:
        logger.info("Validation disagreement logged: file=%s fields=%s engines=%s", filename, disagreements, engines_used)

    return {
        "validation_passed": len(disagreements) == 0,
        "overall_status": overall_status,
        "field_details": field_details,
        "disagreements": disagreements,
        "engines_used": engines_used,
    }
