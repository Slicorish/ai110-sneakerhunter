"""Hard checks on a single Drop.

`errors`   -> the drop is rejected (never scored, never notified).
`warnings` -> the drop survives, but confidence.py subtracts from its score.

The most valuable check here is the evidence check: the snippet the model says
it copied from the page must actually appear in the page text.
"""
from __future__ import annotations

import datetime as dt
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable, Optional
from urllib.parse import urlparse

from . import config
from .schema import DatePrecision, Drop, as_aware


@dataclass
class ValidationResult:
    ok: bool
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "errors": self.errors, "warnings": self.warnings}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _squash(text: str) -> str:
    """Normalize text for evidence matching: unicode, quotes, case, whitespace."""
    text = unicodedata.normalize("NFKC", text)
    text = (
        text.replace("\u2018", "'").replace("\u2019", "'")
        .replace("\u201c", '"').replace("\u201d", '"')
        .replace("\u2013", "-").replace("\u2014", "-")
    )
    return re.sub(r"\s+", " ", text).strip().casefold()


def evidence_in_text(evidence: str, page_text: str) -> bool:
    """True if the evidence snippet appears in the page text (whitespace/case-insensitive)."""
    return _squash(evidence) in _squash(page_text)


def _host_matches(host: str, domains: Iterable[str]) -> bool:
    return any(host == d or host.endswith("." + d) for d in (x.lower().lstrip(".") for x in domains))


# ---------------------------------------------------------------------------
# Individual check groups
# ---------------------------------------------------------------------------
def _check_identity(drop: Drop, errors: list, warnings: list) -> None:
    if not drop.raw_name and not (drop.brand and drop.model):
        errors.append("missing product identity (need raw_name, or brand and model)")
    elif not (drop.brand and drop.model):
        warnings.append("brand/model not separated, so dedupe will be weaker")


def _check_timing(drop: Drop, now: dt.datetime, errors: list, warnings: list) -> None:
    precision = drop.date_precision
    rd = drop.release_datetime

    if rd is None and not drop.date_text:
        errors.append("no release timing (no release_datetime and no date_text)")

    if precision in (DatePrecision.EXACT_DATETIME, DatePrecision.DATE_ONLY) and rd is None:
        errors.append(f"date_precision '{precision.value}' requires release_datetime")
    if precision == DatePrecision.EXACT_DATETIME and rd is not None and rd.tzinfo is None:
        warnings.append("exact time has no timezone; assumed UTC")
    if precision == DatePrecision.MONTH_ONLY and rd is not None:
        if rd.day != 1 or rd.time() != dt.time(0):
            errors.append("month_only precision but release_datetime names a specific day or time")
    if precision == DatePrecision.SEASON and rd is not None:
        errors.append("season precision must not carry a specific release_datetime")
    if precision == DatePrecision.UNKNOWN and rd is not None:
        warnings.append("date_precision is unknown but a release_datetime was given")

    if drop.timezone and rd is not None and rd.tzinfo is None:
        warnings.append(f"timezone {drop.timezone!r} not recognised")

    if rd is None:
        return

    aware = as_aware(rd)
    if precision == DatePrecision.EXACT_DATETIME:
        is_past = aware < now
    elif precision == DatePrecision.DATE_ONLY:
        is_past = aware.date() < now.date()
    else:  # month_only / unknown: compare at month granularity
        is_past = (aware.year, aware.month) < (now.year, now.month)

    if is_past:
        errors.append("release date is in the past")
    elif (aware - now).days > config.MAX_FUTURE_DAYS:
        warnings.append(f"release date is more than {config.MAX_FUTURE_DAYS} days away")


def _check_url(drop: Drop, allowed_domains: Optional[Iterable[str]], errors: list, warnings: list) -> None:
    if not drop.url:
        warnings.append("no url; the notification will have no link")
        return
    parsed = urlparse(drop.url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or "." not in host:
        errors.append(f"url is not a valid http(s) link: {drop.url!r}")
        return
    if allowed_domains and not _host_matches(host, allowed_domains):
        errors.append(f"url domain {host!r} is not in the allowed domains for this source")


def _check_price(drop: Drop, warnings: list) -> None:
    if drop.currency and drop.currency not in config.KNOWN_CURRENCIES:
        warnings.append(f"unknown currency code {drop.currency!r}")
    if drop.price is None:
        return
    if drop.price <= 0:
        warnings.append(f"price {drop.price} is not positive")
        return
    bounds = config.PRICE_RANGE_BY_CURRENCY.get(drop.currency or "")
    if bounds and not (bounds[0] <= drop.price <= bounds[1]):
        warnings.append(f"price {drop.price:g} {drop.currency} is outside the expected {bounds[0]}-{bounds[1]} range")
    if drop.price is not None and not drop.currency:
        warnings.append("price given without a currency")


def _check_style_code(drop: Drop, warnings: list) -> None:
    if not drop.style_code or not drop.brand:
        return
    brand = drop.brand.lower()
    for key, pattern in config.STYLE_CODE_PATTERNS.items():
        if key in brand:
            if not re.match(pattern, drop.style_code.strip().upper()):
                warnings.append(f"style code {drop.style_code!r} does not match the usual {key} format")
            return


def _check_evidence(drop: Drop, page_text: Optional[str], errors: list, warnings: list) -> None:
    if not drop.evidence:
        errors.append("no evidence snippet supplied")
        return
    if page_text is None:
        warnings.append("page text not available; evidence could not be verified")
        return
    if not evidence_in_text(drop.evidence, page_text):
        errors.append("evidence snippet not found in the source page (possible hallucination)")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def validate(
    drop: Drop,
    page_text: Optional[str],
    now: dt.datetime,
    allowed_domains: Optional[Iterable[str]] = None,
) -> ValidationResult:
    """Validate one drop against the page it came from.

    `allowed_domains` should be passed only for sources whose links are expected
    to stay on their own domain (an official retailer page). Leave it None for
    blogs, which legitimately link out to retailers.
    """
    now = as_aware(now)
    errors: list = []
    warnings: list = []

    _check_identity(drop, errors, warnings)
    _check_timing(drop, now, errors, warnings)
    _check_url(drop, allowed_domains, errors, warnings)
    _check_price(drop, warnings)
    _check_style_code(drop, warnings)
    _check_evidence(drop, page_text, errors, warnings)
    if drop.model_confidence == "low":
        warnings.append("model reported low confidence in this extraction")

    return ValidationResult(ok=not errors, errors=errors, warnings=warnings)