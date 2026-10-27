"""Reliability layer: validate, dedupe, score, and track drops. No LLM calls here."""
from .changes import (
    ChangeResult, FieldChange, SeenStore, commit, detect_change, drop_key,
    mark_missing, snapshot,
)
from .confidence import ConfidenceResult, score_drop
from .dedupe import (
    AmbiguousPair, DedupeResult, dedupe, match_level, merge_drops,
    normalize_style_code, normalize_text, resolve_ambiguous,
)
from .schema import (
    DatePrecision, Drop, ReleaseType, SchemaError, SourceRef, Tier,
)
from .validators import ValidationResult, evidence_in_text, validate

__all__ = [
    "AmbiguousPair", "ChangeResult", "ConfidenceResult", "DatePrecision",
    "DedupeResult", "Drop", "FieldChange", "ReleaseType", "SchemaError",
    "SeenStore", "SourceRef", "Tier", "ValidationResult", "commit", "dedupe",
    "detect_change", "drop_key", "evidence_in_text", "mark_missing",
    "match_level", "merge_drops", "normalize_style_code", "normalize_text",
    "resolve_ambiguous", "score_drop", "snapshot", "validate",
]