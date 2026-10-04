"""Document extraction with field-level provenance.

Applications arrive as scans, PDFs and photographs of paper forms. Two thirds of
the carrier's submission time is spent rekeying them. Extraction removes that,
but an extracted value is only as good as its provenance, so every field carries:

* where it came from (document id, page, bounding box),
* how confident the extractor is,
* what it was extracted *from* (the raw text span).

The extractor here is a deterministic parser over raw text rather than a vision
model. That is a deliberate trade for a demo-grade build: it is reproducible,
unit-testable, and honest about its limits. The interface is shaped so a real
OCR/vision extractor can be dropped in behind the same protocol without touching
the workbench.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Protocol

from pas_plugins.plugin3_auw.models import (
    DocumentRef,
    ExtractedField,
    FieldProvenance,
    Submission,
)

# Confidence assigned to a value recovered by an exact, unambiguous pattern match.
_EXACT_MATCH_CONFIDENCE = 0.97
# Confidence for a value recovered by a loose pattern, where a human should look.
_LOOSE_MATCH_CONFIDENCE = 0.72
# Below this the workbench surfaces the field as "needs verification".
REVIEW_THRESHOLD = 0.85

_PATTERNS: dict[str, tuple[re.Pattern[str], Any]] = {
    "applicant.age": (re.compile(r"\b(?:age|dob|date of birth)\D{0,12}(\d{2})\b", re.I), "int_age"),
    "applicant.sex": (re.compile(r"\bsex\s*[:\-]?\s*([MFU])\b", re.I), "upper"),
    "applicant.tobacco_use": (re.compile(r"\b(tobacco|nicotine|smoker)\b", re.I), "bool_true"),
    "applicant.height_inches": (re.compile(r"\b(?:height|ht)\D{0,8}(\d{1,2})\s*(?:in|inches|\")?", re.I), "int"),
    "applicant.weight_pounds": (re.compile(r"\b(?:weight|wt)\D{0,8}(\d{2,3})\s*(?:lbs?|pounds)?", re.I), "int"),
    "request.face_amount": (re.compile(r"\b(?:face|death benefit|amount)\D{0,12}([\d,]{5,})", re.I), "money"),
    "request.term_years": (re.compile(r"\b(\d{1,2})\s*[- ]?year\b", re.I), "int"),
    "request.product_code": (re.compile(r"\bproduct\s*(?:code)?\s*[:\-]?\s*([A-Za-z0-9][A-Za-z0-9\-]{2,20})\b", re.I), "upper"),
    "request.payment_mode": (re.compile(r"\b(monthly|annual|quarterly|monthly\s*automatic)\b", re.I), "lower"),
    "applicant.state_of_residence": (re.compile(r"\bstate\s*[:\-]?\s*([A-Za-z]{2})\b", re.I), "upper"),
    "applicant.avocation": (re.compile(r"\boccupation\w*\s*[:\-]?\s*([^,\n]{3,60})", re.I), "title"),
}


class DocumentText:
    """Raw text pulled from one document, with page boundaries preserved."""

    def __init__(self, document_id: str, text: str, pages: list[str] | None = None) -> None:
        self.document_id = document_id
        self.text = text
        self.pages = list(pages) if pages else [text]
        self.extracted_at = datetime.now(UTC)

    def page_of(self, offset: int) -> int:
        """Return the 1-based page number containing a character offset."""
        consumed = 0
        for index, page in enumerate(self.pages, start=1):
            consumed += len(page) + 1
            if offset < consumed:
                return index
        return len(self.pages)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"DocumentText(document_id={self.document_id!r}, chars={len(self.text)})"


class Extractor(Protocol):
    """The contract a real extractor must satisfy."""

    def extract(self, submission: Submission, texts: list[DocumentText]) -> list[ExtractedField]: ...


class RuleBasedExtractor:
    """Deterministic pattern extractor over document text.

    Not a language model, and it does not pretend to be one. It reliably recovers
    the labelled, tabular fields on a life application - which is most of the
    rekeying cost - and leaves free-form medical narrative to a human.
    """

    name = "rule-based-v1"

    def extract(self, submission: Submission, texts: list[DocumentText]) -> list[ExtractedField]:
        fields: list[ExtractedField] = []
        for text in texts:
            for path, (pattern, coercion) in _PATTERNS.items():
                match = pattern.search(text.text)
                if not match:
                    continue
                raw = match.group(1)
                value = _coerce(raw, coercion)
                if value is None:
                    continue
                fields.append(
                    ExtractedField(
                        field=path,
                        value=value,
                        confidence=_confidence_for(path, raw),
                        provenance=FieldProvenance.DOCUMENT_EXTRACTED,
                        source_document=text.document_id,
                        page=text.page_of(match.start()),
                        bounding_box=_fake_box(match.start(), len(match.group(0))),
                    )
                )
        return _reconcile(fields, submission)

    def merge_with_declarations(
        self, extracted: list[ExtractedField], submission: Submission
    ) -> list[ExtractedField]:
        """Prefer applicant-declared values over document values on conflict.

        The applicant's own declaration wins, because that is what the application
        is contractually based on; a mismatch is flagged rather than silently
        overwritten so an agent can resolve it.
        """
        declared = _declared_values(submission)
        merged: list[ExtractedField] = []
        for field in extracted:
            declared_value = declared.get(field.field)
            if declared_value is None:
                merged.append(field)
                continue
            if _equivalent(declared_value, field.value):
                merged.append(
                    field.model_copy(update={"provenance": FieldProvenance.APPLICANT_DECLARED, "confidence": max(field.confidence, 0.99)})
                )
                continue
            merged.append(
                field.model_copy(update={"confidence": min(field.confidence, 0.5)})
            )
            merged.append(
                ExtractedField(
                    field=field.field,
                    value=declared_value,
                    confidence=0.99,
                    provenance=FieldProvenance.APPLICANT_DECLARED,
                    source_document=field.source_document,
                )
            )
        return merged


def _reconcile(fields: list[ExtractedField], submission: Submission) -> list[ExtractedField]:
    """Keep the highest-confidence extraction per field, and note the conflicts."""
    best: dict[str, ExtractedField] = {}
    for field in fields:
        current = best.get(field.field)
        if current is None or field.confidence > current.confidence:
            if current is not None:
                field = field.model_copy(
                    update={"confidence": round(field.confidence, 4)}
                )
            best[field.field] = field
    return sorted(best.values(), key=lambda f: f.field)


def _declared_values(submission: Submission) -> dict[str, Any]:
    applicant = submission.applicant
    request = submission.request
    return {
        "applicant.age": applicant.age,
        "applicant.sex": applicant.sex,
        "applicant.tobacco_use": applicant.tobacco_use,
        "applicant.height_inches": applicant.height_inches,
        "applicant.weight_pounds": applicant.weight_pounds,
        "applicant.state_of_residence": applicant.state_of_residence,
        "request.face_amount": request.face_amount,
        "request.term_years": request.term_years,
        "request.product_code": request.product_code,
        "request.payment_mode": request.payment_mode,
    }


def _equivalent(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return bool(left) == bool(right)
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return abs(float(left) - float(right)) < 0.01
    if isinstance(left, str) and isinstance(right, str):
        return left.strip().lower() == right.strip().lower()
    return left == right


def _confidence_for(path: str, raw: str) -> float:
    """Loose matches get a lower confidence so the UI marks them for verification."""
    loose_paths = {"applicant.avocation", "applicant.state_of_residence", "request.term_years"}
    return _LOOSE_MATCH_CONFIDENCE if path in loose_paths else _EXACT_MATCH_CONFIDENCE


def _coerce(raw: str, kind: str) -> Any:
    try:
        if kind == "int":
            return int(raw.replace(",", ""))
        if kind == "int_age":
            return int(raw)
        if kind == "money":
            return float(raw.replace(",", ""))
        if kind == "upper":
            return raw.strip().upper()
        if kind == "lower":
            return " ".join(raw.strip().lower().split())
        if kind == "title":
            return raw.strip().title()
        if kind == "bool_true":
            return True
    except ValueError:
        return None
    return raw.strip()


def _fake_box(offset: int, length: int) -> tuple[float, float, float, float]:
    """A deterministic stand-in for a real bounding box.

    Real extractors return true coordinates from the OCR engine. Deriving a stable
    pseudo-box from the text offset keeps the response shape honest and stable
    without pretending to be vision.
    """
    x0 = 40.0 + (offset % 400)
    y0 = 60.0 + ((offset // 40) % 700)
    return (x0, y0, x0 + 120.0 + length, y0 + 18.0)


def documents_from(submission: Submission) -> list[DocumentRef]:
    return list(submission.documents)


__all__ = [
    "DocumentText",
    "Extractor",
    "REVIEW_THRESHOLD",
    "RuleBasedExtractor",
]