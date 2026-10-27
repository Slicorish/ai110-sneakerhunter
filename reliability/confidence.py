"""Score a validated, deduped Drop and assign a tier.

The score is a transparent sum of factor impacts. Hard caps are applied AFTER
scoring so rules like "a rumor can never be 'high'" cannot be outvoted by a
big score. Every factor carries a human-readable `note`, which feeds both the
fallback explanation and prompts/explain_confidence.txt.

Deterministic: the same input always gives the same output.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import config
from .schema import DatePrecision, Drop, ReleaseType, Tier
from .validators import ValidationResult


@dataclass
class ConfidenceResult:
    score: float
    tier: Tier
    factors: list = field(default_factory=list)  # list[dict], see _factor()

    def to_dict(self) -> dict:
        return {"score": self.score, "tier": self.tier.value, "factors": self.factors}


def _factor(name, value, status, impact, note, decisive=False) -> dict:
    return {
        "factor": name,
        "value": value,
        "status": status,        # "strong" | "ok" | "weak" | "cap"
        "impact": round(impact, 2),
        "note": note,
        "decisive": decisive,    # True when a hard rule set the tier
    }


def _status(impact: float) -> str:
    if impact >= 0.15:
        return "strong"
    if impact < 0:
        return "weak"
    return "ok"


def _tier_for(score: float) -> Tier:
    for minimum, name in config.TIERS:
        if score >= minimum:
            return Tier(name)
    return Tier.LOG_ONLY


def score_drop(drop: Drop, validation: ValidationResult) -> ConfidenceResult:
    """Compute score, tier and explanatory factors for one drop."""
    factors: list = []
    score = config.BASE_SCORE

    # A drop that failed validation should never get this far, but guard anyway.
    if not validation.ok:
        factors.append(_factor(
            "validation", "failed", "cap", 0.0,
            "Failed validation: " + "; ".join(validation.errors[:2]), decisive=True))
        return ConfidenceResult(0.0, Tier.LOG_ONLY, factors)

    # 1. Corroboration: count INDEPENDENT sources (syndicated copies count once).
    groups = {s.independence_key for s in drop.sources}
    n_independent = max(len(groups), 1)
    note = f"Reported by {n_independent} independent source{'s' if n_independent != 1 else ''}"
    if len(drop.sources) > n_independent:
        note += f" ({len(drop.sources)} reports, some syndicated)"
    if drop.date_conflicts and n_independent > 1:
        # Sources that contradict each other on the date do not corroborate it.
        impact = config.SOURCE_COUNT_IMPACT[1]
        note += ", but they disagree on the date, so no corroboration credit"
    else:
        impact = config.SOURCE_COUNT_IMPACT[min(n_independent, 3)]
    factors.append(_factor("source_count", n_independent, _status(impact), impact, note))
    score += impact

    # 2. Trust of the best source.
    best = drop.best_trust if drop.sources else config.DEFAULT_TRUST
    best_label = max(drop.sources, key=lambda s: s.trust_score).trust if drop.sources else "unknown"
    impact = (best - 0.5) * config.TRUST_WEIGHT
    factors.append(_factor(
        "source_trust", best_label, _status(impact), impact,
        f"Best source is '{best_label}' (trust {best:.1f})"))
    score += impact

    # 3. Date precision.
    impact = config.PRECISION_IMPACT[drop.date_precision.value]
    notes = {
        DatePrecision.EXACT_DATETIME: "An exact release date and time is given",
        DatePrecision.DATE_ONLY: "A release date is given but no time",
        DatePrecision.MONTH_ONLY: "Only the release month is given, not an exact date",
        DatePrecision.SEASON: "Only a season or vague timeframe is given",
        DatePrecision.UNKNOWN: "The release timing is unclear",
    }
    factors.append(_factor("date_precision", drop.date_precision.value, _status(impact), impact,
                           notes[drop.date_precision]))
    score += impact

    # 4. Release type (a page classified as a rumor counts as a rumor).
    is_rumor = drop.release_type == ReleaseType.RUMOR or drop.page_label == "rumor_or_leak"
    type_key = "rumor" if is_rumor else drop.release_type.value
    impact = config.RELEASE_TYPE_IMPACT[type_key]
    if is_rumor and drop.release_type != ReleaseType.RUMOR:
        note = "The source page was classified as a rumor or leak"
    else:
        note = f"Release type is {type_key.replace('_', ' ')}"
    factors.append(_factor("release_type", type_key, _status(impact), impact, note))
    score += impact

    # 5. How the data was extracted.
    impact = config.EXTRACTION_PATH_IMPACT[drop.extraction_path]
    path_notes = {
        "llm": "Extracted by the model and passed validation",
        "heuristic": "Extracted by rule-based parsing and passed validation",
        "llm_repaired": "Model output needed repair before it could be read",
    }
    factors.append(_factor("extraction_path", drop.extraction_path, _status(impact), impact,
                           path_notes[drop.extraction_path]))
    score += impact

    # 6. The model's own confidence (weak signal; can only subtract).
    if drop.model_confidence:
        impact = config.MODEL_CONFIDENCE_IMPACT[drop.model_confidence]
        factors.append(_factor("model_confidence", drop.model_confidence, _status(impact), impact,
                               f"The extraction model reported {drop.model_confidence} confidence"))
        score += impact

    # 7. Validation warnings.
    if validation.warnings:
        impact = -min(len(validation.warnings) * config.WARNING_PENALTY, config.WARNING_PENALTY_MAX)
        factors.append(_factor("validation_warnings", len(validation.warnings), "weak", impact,
                               "Minor issues: " + "; ".join(validation.warnings[:2])))
        score += impact

    # 8. Sources disagree on the date.
    if drop.date_conflicts:
        impact = -config.DATE_CONFLICT_PENALTY
        factors.append(_factor("date_conflict", len(drop.date_conflicts), "weak", impact,
                               "Sources disagree on the release date"))
        score += impact

    score = round(min(max(score, 0.0), 1.0), 2)
    tier = _tier_for(score)

    # Hard caps: applied after scoring, only ever lower the tier.
    caps = []
    if is_rumor:
        caps.append(("cap_rumor", "Rumors are never rated higher than unconfirmed"))
    if drop.date_precision.value in config.CAP_PRECISIONS:
        caps.append(("cap_vague_date", "Without an exact date this cannot be rated high"))
    if drop.date_conflicts:
        caps.append(("cap_date_conflict", "Conflicting dates keep this unconfirmed"))
    if n_independent == 1 and best < config.HIGH_TRUST_MIN:
        caps.append(("cap_single_source", "A single lower-trust source cannot be rated high"))

    pre_cap = tier
    if pre_cap == Tier.HIGH and caps:
        tier = Tier.UNCONFIRMED
        for name, note in caps:
            factors.append(_factor(name, "capped", "cap", 0.0, note, decisive=True))

    # Decisive factors first, then largest absolute impact.
    factors.sort(key=lambda f: (not f["decisive"], -abs(f["impact"])))
    return ConfidenceResult(score=score, tier=tier, factors=factors)