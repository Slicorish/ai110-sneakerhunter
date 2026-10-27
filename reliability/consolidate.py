"""Collapse reports of the same shoe from different sources into one Drop.

Policy: a false merge hides a real drop, so when in doubt, do NOT merge.
Clear matches merge automatically. Near-matches are returned as "ambiguous"
pairs for an optional LLM tiebreaker (prompts/resolve_duplicates.txt) or for
the caller to leave separate.
"""
from __future__ import annotations

import copy
import difflib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

from . import config
from .schema import DatePrecision, Drop, PRECISION_RANK, as_aware

_ALIASES = [(re.compile(pattern), repl) for pattern, repl in config.ALIAS_PATTERNS]
_PRECISE = (DatePrecision.EXACT_DATETIME, DatePrecision.DATE_ONLY)


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------
def normalize_text(value: Optional[str]) -> str:
    """Lowercase, strip accents/punctuation, expand aliases, drop collab noise."""
    if not value:
        return ""
    text = value.lower().replace("\u00d7", " x ")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    for pattern, repl in _ALIASES:
        text = pattern.sub(repl, text)
    text = re.sub(r"[^\w\s]", " ", text)
    return " ".join(t for t in text.split() if t not in config.NOISE_TOKENS)


def normalize_style_code(value: Optional[str]) -> str:
    """'DZ5485-612' and 'dz5485 612' both become 'DZ5485612'."""
    return re.sub(r"[^A-Z0-9]", "", value.upper()) if value else ""


def _full_name(drop: Drop) -> str:
    if drop.brand or drop.model:
        return normalize_text(" ".join(p for p in (drop.brand, drop.model, drop.colorway) if p))
    return normalize_text(drop.raw_name)


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------
def _date_relation(a: Drop, b: Drop) -> str:
    """'close' | 'conflict' | 'far' | 'unknown'.

    Only exact/date-level dates on BOTH drops are compared. Anything vaguer
    ("March 2027") is 'unknown' so it can never contradict an exact date.
    """
    if (
        a.release_datetime is None or b.release_datetime is None
        or a.date_precision not in _PRECISE or b.date_precision not in _PRECISE
    ):
        return "unknown"
    diff = abs(as_aware(a.release_datetime) - as_aware(b.release_datetime))
    if diff.total_seconds() <= config.DATE_MATCH_TOLERANCE_HOURS * 3600:
        return "close"
    if diff.days <= config.DATE_CONFLICT_WINDOW_DAYS:
        return "conflict"
    return "far"


def match_level(a: Drop, b: Drop) -> str:
    """Return 'same', 'ambiguous' or 'different'."""
    relation = _date_relation(a, b)

    # 1. Style codes are the strongest signal.
    code_a, code_b = normalize_style_code(a.style_code), normalize_style_code(b.style_code)
    if code_a and code_b:
        if code_a != code_b:
            return "different"
        return "different" if relation == "far" else "same"  # far apart = restock/re-release

    # 2. Normalized brand + model (+ colorway).
    brand_a, brand_b = normalize_text(a.brand), normalize_text(b.brand)
    model_a, model_b = normalize_text(a.model), normalize_text(b.model)
    color_a, color_b = normalize_text(a.colorway), normalize_text(b.colorway)

    if model_a and model_a == model_b and brand_a == brand_b:
        if color_a and color_b:
            if color_a != color_b:
                return "different"          # different colorways are different products
            return "different" if relation == "far" else "same"
        # One side lacks a colorway: probably the same shoe, but not sure.
        return "different" if relation == "far" else "ambiguous"

    # 3. Fuzzy name similarity is never enough on its own.
    name_a, name_b = _full_name(a), _full_name(b)
    if name_a and name_b and relation != "far":
        if difflib.SequenceMatcher(None, name_a, name_b).ratio() >= config.FUZZY_THRESHOLD:
            return "ambiguous"
    return "different"


# ---------------------------------------------------------------------------
# Merging
# ---------------------------------------------------------------------------
_FILL_FIELDS = (
    "brand", "model", "colorway", "raw_name", "style_code", "price",
    "currency", "retailer", "url", "timezone",
)


def _lead_source(drop: Drop) -> str:
    if not drop.sources:
        return "unknown"
    return max(drop.sources, key=lambda s: s.trust_score).source_id


def _conflict_entry(drop: Drop) -> dict:
    return {"source_id": _lead_source(drop), "release_datetime": drop.release_datetime.isoformat()}


def merge_drops(a: Drop, b: Drop) -> Drop:
    """Merge two drops. The higher-trust, higher-precision one wins conflicts."""
    primary, other = (a, b) if (a.best_trust, PRECISION_RANK[a.date_precision]) >= (
        b.best_trust, PRECISION_RANK[b.date_precision]) else (b, a)
    merged = copy.deepcopy(primary)

    for name in _FILL_FIELDS:
        if not getattr(merged, name) and getattr(other, name):
            setattr(merged, name, getattr(other, name))

    # Adopt the other drop's date only if the primary has no concrete date.
    if merged.release_datetime is None and other.release_datetime is not None:
        merged.release_datetime = other.release_datetime
        merged.date_precision = other.date_precision
        merged.date_text = other.date_text
        merged.timezone = other.timezone or merged.timezone

    # Record disagreement instead of silently picking a winner.
    if _date_relation(primary, other) in ("conflict", "far"):
        known = {(c["source_id"], c["release_datetime"]) for c in merged.date_conflicts}
        for entry in [*primary.date_conflicts, *other.date_conflicts,
                      _conflict_entry(primary), _conflict_entry(other)]:
            key = (entry["source_id"], entry["release_datetime"])
            if key not in known:
                merged.date_conflicts.append(entry)
                known.add(key)
    else:
        for entry in other.date_conflicts:
            if entry not in merged.date_conflicts:
                merged.date_conflicts.append(entry)

    seen = {(s.source_id, s.url) for s in merged.sources}
    for source in other.sources:
        if (source.source_id, source.url) not in seen:
            merged.sources.append(copy.deepcopy(source))
            seen.add((source.source_id, source.url))
    return merged


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
@dataclass
class AmbiguousPair:
    a_index: int
    b_index: int


@dataclass
class DedupeResult:
    merged: list = field(default_factory=list)       # list[Drop]
    ambiguous: list = field(default_factory=list)    # list[AmbiguousPair]

    def pair_drops(self, pair: AmbiguousPair):
        return self.merged[pair.a_index], self.merged[pair.b_index]


def dedupe(drops: list) -> DedupeResult:
    """Cluster drops. Input drops are not mutated."""
    clusters: list = []
    ambiguous: list = []
    for drop in drops:
        pending: list = []
        placed = False
        for i, cluster in enumerate(clusters):
            level = match_level(cluster, drop)
            if level == "same":
                clusters[i] = merge_drops(cluster, drop)
                placed = True
                break
            if level == "ambiguous":
                pending.append(i)
        if not placed:
            clusters.append(copy.deepcopy(drop))
            ambiguous.extend(AmbiguousPair(i, len(clusters) - 1) for i in pending)
    return DedupeResult(merged=clusters, ambiguous=ambiguous)


def resolve_ambiguous(result: DedupeResult, verdicts: list) -> DedupeResult:
    """Apply tiebreaker verdicts ('same' | 'different' | 'unsure') to ambiguous pairs.

    `verdicts[i]` answers `result.ambiguous[i]`. Only 'same' merges; 'unsure'
    and 'different' leave the drops separate. Returns a new DedupeResult with
    no ambiguous pairs left.
    """
    if len(verdicts) != len(result.ambiguous):
        raise ValueError("need exactly one verdict per ambiguous pair")

    parent = list(range(len(result.merged)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for pair, verdict in zip(result.ambiguous, verdicts):
        if verdict == "same":
            parent[find(pair.b_index)] = find(pair.a_index)

    groups: dict = {}
    for index, drop in enumerate(result.merged):
        groups.setdefault(find(index), []).append(drop)

    merged = []
    for members in groups.values():
        combined = members[0]
        for other in members[1:]:
            combined = merge_drops(combined, other)
        merged.append(combined)
    return DedupeResult(merged=merged, ambiguous=[])