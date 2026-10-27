"""The Drop contract shared by prompts, validators, dedupe, scoring and tests.

`Drop.from_dict` is the single gate between untrusted LLM/heuristic output and
the rest of the pipeline. Anything that does not match the contract raises
`SchemaError`, so downstream code can assume clean types.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import config


class SchemaError(ValueError):
    """Extraction output does not match the Drop contract."""


class DatePrecision(str, Enum):
    EXACT_DATETIME = "exact_datetime"
    DATE_ONLY = "date_only"
    MONTH_ONLY = "month_only"
    SEASON = "season"
    UNKNOWN = "unknown"


class ReleaseType(str, Enum):
    GENERAL_RELEASE = "general_release"
    RAFFLE = "raffle"
    DRAW = "draw"
    RESTOCK = "restock"
    RUMOR = "rumor"
    UNKNOWN = "unknown"


class Tier(str, Enum):
    HIGH = "high"
    UNCONFIRMED = "unconfirmed"
    LOG_ONLY = "log_only"

    @property
    def rank(self) -> int:
        return {"log_only": 0, "unconfirmed": 1, "high": 2}[self.value]


PRECISION_RANK = {
    DatePrecision.EXACT_DATETIME: 4,
    DatePrecision.DATE_ONLY: 3,
    DatePrecision.MONTH_ONLY: 2,
    DatePrecision.SEASON: 1,
    DatePrecision.UNKNOWN: 0,
}

EXTRACTION_PATHS = {"llm", "llm_repaired", "heuristic"}
MODEL_CONFIDENCES = {"high", "medium", "low"}

# Strings an LLM sometimes emits instead of null. Treated as null.
_PLACEHOLDERS = {"n/a", "na", "tbd", "tba", "null", "none", "unknown", "-", ""}


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------
def as_aware(value: dt.datetime) -> dt.datetime:
    """Return a timezone-aware datetime. Naive values are assumed to be UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value


_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")


def parse_datetime(value: Any, tz_name: Optional[str] = None) -> Optional[dt.datetime]:
    """Parse ISO 8601 (also 'YYYY-MM-DD' and 'YYYY-MM') into a datetime.

    If the value has no UTC offset and `tz_name` is a valid IANA zone, the zone
    is attached. An unrecognised zone name leaves the datetime naive; the
    validator reports that as a warning.
    """
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if text.lower() in _PLACEHOLDERS:
            return None
        try:
            month = _MONTH_RE.match(text)
            if month:
                parsed = dt.datetime(int(month.group(1)), int(month.group(2)), 1)
            else:
                if text[-1] in "Zz":
                    text = text[:-1] + "+00:00"
                parsed = dt.datetime.fromisoformat(text)
        except ValueError as exc:
            raise SchemaError(f"unparseable release_datetime: {value!r}") from exc
    else:
        raise SchemaError(f"release_datetime must be a string, got {type(value).__name__}")

    if parsed.tzinfo is None and tz_name:
        try:
            parsed = parsed.replace(tzinfo=ZoneInfo(tz_name))
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return parsed


# ---------------------------------------------------------------------------
# Source reference
# ---------------------------------------------------------------------------
@dataclass
class SourceRef:
    """Where a drop was reported. Fields mirror an entry in sources.yaml."""

    source_id: str
    url: Optional[str] = None
    trust: str = "aggregator"
    group: Optional[str] = None  # sources sharing a group count as ONE source

    @property
    def trust_score(self) -> float:
        return config.SOURCE_TRUST.get(self.trust, config.DEFAULT_TRUST)

    @property
    def independence_key(self) -> str:
        return self.group or self.source_id

    def to_dict(self) -> dict:
        return {
            "source_id": self.source_id,
            "url": self.url,
            "trust": self.trust,
            "group": self.group,
        }


# ---------------------------------------------------------------------------
# Drop
# ---------------------------------------------------------------------------
_EXTRACTED_FIELDS = {
    "brand", "model", "colorway", "raw_name", "style_code",
    "release_datetime", "date_precision", "date_text", "timezone",
    "release_type", "price", "currency", "retailer", "url",
    "evidence", "extraction_notes", "model_confidence",
}


def _opt_str(data: dict, key: str) -> Optional[str]:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise SchemaError(f"{key} must be a string or null, got {type(value).__name__}")
    value = value.strip()
    return None if value.lower() in _PLACEHOLDERS else value


def _enum(data: dict, key: str, enum_cls, default):
    value = data.get(key)
    if value is None:
        return default
    if not isinstance(value, str):
        raise SchemaError(f"{key} must be a string, got {type(value).__name__}")
    try:
        return enum_cls(value.strip().lower())
    except ValueError as exc:
        allowed = [e.value for e in enum_cls]
        raise SchemaError(f"{key}={value!r} is not one of {allowed}") from exc


def _price(data: dict) -> Optional[float]:
    value = data.get("price")
    if value is None:
        return None
    if isinstance(value, bool):
        raise SchemaError("price must be a number, got bool")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "").lstrip("$€£¥")
        if cleaned.lower() in _PLACEHOLDERS:
            return None
        try:
            return float(cleaned)
        except ValueError as exc:
            raise SchemaError(f"unparseable price: {value!r}") from exc
    raise SchemaError(f"price must be a number, got {type(value).__name__}")


@dataclass
class Drop:
    # --- fields produced by extraction (match prompts/extract_drop.txt) ---
    brand: Optional[str] = None
    model: Optional[str] = None
    colorway: Optional[str] = None
    raw_name: Optional[str] = None
    style_code: Optional[str] = None
    release_datetime: Optional[dt.datetime] = None
    date_precision: DatePrecision = DatePrecision.UNKNOWN
    date_text: Optional[str] = None   # original wording, e.g. "Spring 2027"
    timezone: Optional[str] = None
    release_type: ReleaseType = ReleaseType.UNKNOWN
    price: Optional[float] = None
    currency: Optional[str] = None
    retailer: Optional[str] = None
    url: Optional[str] = None
    evidence: Optional[str] = None
    extraction_notes: Optional[str] = None
    model_confidence: Optional[str] = None

    # --- fields added by the pipeline ---
    sources: list = field(default_factory=list)          # list[SourceRef]
    extraction_path: str = "llm"
    page_label: Optional[str] = None                     # from classify_page
    date_conflicts: list = field(default_factory=list)   # set by dedupe

    # ------------------------------------------------------------------
    @classmethod
    def from_dict(
        cls,
        data: dict,
        source: Optional[SourceRef] = None,
        extraction_path: str = "llm",
        page_label: Optional[str] = None,
    ) -> "Drop":
        """Build a Drop from one item of the extractor's JSON, or raise SchemaError."""
        if not isinstance(data, dict):
            raise SchemaError(f"drop must be an object, got {type(data).__name__}")
        unknown = set(data) - _EXTRACTED_FIELDS
        if unknown:
            raise SchemaError(f"unknown fields: {sorted(unknown)}")
        if extraction_path not in EXTRACTION_PATHS:
            raise SchemaError(f"extraction_path must be one of {sorted(EXTRACTION_PATHS)}")

        tz_name = _opt_str(data, "timezone")
        confidence = _opt_str(data, "model_confidence")
        if confidence is not None:
            confidence = confidence.lower()
            if confidence not in MODEL_CONFIDENCES:
                raise SchemaError(f"model_confidence={confidence!r} is not high/medium/low")
        currency = _opt_str(data, "currency")

        return cls(
            brand=_opt_str(data, "brand"),
            model=_opt_str(data, "model"),
            colorway=_opt_str(data, "colorway"),
            raw_name=_opt_str(data, "raw_name"),
            style_code=_opt_str(data, "style_code"),
            release_datetime=parse_datetime(data.get("release_datetime"), tz_name),
            date_precision=_enum(data, "date_precision", DatePrecision, DatePrecision.UNKNOWN),
            date_text=_opt_str(data, "date_text"),
            timezone=tz_name,
            release_type=_enum(data, "release_type", ReleaseType, ReleaseType.UNKNOWN),
            price=_price(data),
            currency=currency.upper() if currency else None,
            retailer=_opt_str(data, "retailer"),
            url=_opt_str(data, "url"),
            evidence=_opt_str(data, "evidence"),
            extraction_notes=_opt_str(data, "extraction_notes"),
            model_confidence=confidence,
            sources=[source] if source else [],
            extraction_path=extraction_path,
            page_label=page_label,
        )

    # ------------------------------------------------------------------
    @property
    def display_name(self) -> str:
        parts = [p for p in (self.brand, self.model, self.colorway) if p]
        return " ".join(parts) if parts else (self.raw_name or "(unnamed drop)")

    @property
    def best_trust(self) -> float:
        return max((s.trust_score for s in self.sources), default=0.0)

    def to_dict(self) -> dict:
        return {
            "brand": self.brand,
            "model": self.model,
            "colorway": self.colorway,
            "raw_name": self.raw_name,
            "style_code": self.style_code,
            "release_datetime": self.release_datetime.isoformat() if self.release_datetime else None,
            "date_precision": self.date_precision.value,
            "date_text": self.date_text,
            "timezone": self.timezone,
            "release_type": self.release_type.value,
            "price": self.price,
            "currency": self.currency,
            "retailer": self.retailer,
            "url": self.url,
            "evidence": self.evidence,
            "extraction_notes": self.extraction_notes,
            "model_confidence": self.model_confidence,
            "sources": [s.to_dict() for s in self.sources],
            "extraction_path": self.extraction_path,
            "page_label": self.page_label,
            "date_conflicts": list(self.date_conflicts),
        }