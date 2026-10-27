"""Every tunable knob for the reliability layer lives here.

Change numbers in this file, not in the logic modules. Tests and tuning
experiments should only ever need to touch config.py.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Source trust
# ---------------------------------------------------------------------------
# Labels used in sources.yaml -> trust score between 0 and 1.
SOURCE_TRUST = {
    "retailer_official": 1.0,
    "reputable_blog": 0.6,
    "aggregator": 0.4,
    "rumor": 0.2,
}
DEFAULT_TRUST = 0.3          # used when a source has an unknown trust label
HIGH_TRUST_MIN = 0.8         # a lone source needs at least this to reach "high"

# ---------------------------------------------------------------------------
# Confidence scoring
# ---------------------------------------------------------------------------
BASE_SCORE = 0.5

# (minimum score, tier name), checked top to bottom.
TIERS = [(0.75, "high"), (0.45, "unconfirmed"), (0.0, "log_only")]

# Impact of the number of INDEPENDENT sources (3 means "3 or more").
SOURCE_COUNT_IMPACT = {1: -0.15, 2: 0.10, 3: 0.20}

# impact = (best_trust - 0.5) * TRUST_WEIGHT  ->  official +0.25, rumor -0.15
TRUST_WEIGHT = 0.5

PRECISION_IMPACT = {
    "exact_datetime": 0.15,
    "date_only": 0.05,
    "month_only": -0.15,
    "season": -0.25,
    "unknown": -0.30,
}

RELEASE_TYPE_IMPACT = {
    "general_release": 0.05,
    "raffle": 0.05,
    "draw": 0.05,
    "restock": 0.0,
    "rumor": -0.25,
    "unknown": -0.10,
}

EXTRACTION_PATH_IMPACT = {
    "llm": 0.0,
    "heuristic": 0.0,
    "llm_repaired": -0.10,   # JSON needed fixing before it parsed
}

# Self-reported model confidence is a weak signal, so it only subtracts.
MODEL_CONFIDENCE_IMPACT = {"high": 0.0, "medium": 0.0, "low": -0.10}

WARNING_PENALTY = 0.05       # per validation warning
WARNING_PENALTY_MAX = 0.20
DATE_CONFLICT_PENALTY = 0.20

# Tiers that are NEVER allowed past a cap, whatever the score says.
CAP_PRECISIONS = {"month_only", "season", "unknown"}

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
MAX_FUTURE_DAYS = 730

KNOWN_CURRENCIES = {
    "USD", "EUR", "GBP", "CAD", "AUD", "JPY", "CNY", "KRW", "MXN", "CHF",
    "SEK", "SGD", "HKD", "NZD", "INR", "BRL",
}

# Sane retail price window per currency. Currencies not listed skip the check.
PRICE_RANGE_BY_CURRENCY = {
    "USD": (20, 1500),
    "EUR": (20, 1500),
    "GBP": (20, 1200),
    "CAD": (25, 2000),
    "AUD": (30, 2000),
    "JPY": (3_000, 250_000),
    "KRW": (30_000, 2_000_000),
}

# Optional per-brand style-code formats (checked only as a WARNING).
# The key is matched as a substring of the normalized brand name.
STYLE_CODE_PATTERNS = {
    "nike": r"^[A-Z0-9]{5,6}-\d{3}$",
    "jordan": r"^[A-Z0-9]{5,6}-\d{3}$",
    "adidas": r"^[A-Z]{1,2}\d{4,5}$",
}

# ---------------------------------------------------------------------------
# Dedupe
# ---------------------------------------------------------------------------
DATE_MATCH_TOLERANCE_HOURS = 24     # dates this close count as the same date
DATE_CONFLICT_WINDOW_DAYS = 14      # same shoe, dates this far apart = conflict
                                    # beyond this, assume a separate release
FUZZY_THRESHOLD = 0.85              # name similarity that makes a pair ambiguous

# Applied to lowercased text before punctuation is stripped.
ALIAS_PATTERNS = [
    (r"\baj\s?(\d+)\b", r"air jordan \1"),
    (r"(?<!air )\bjordan (\d+)\b", r"air jordan \1"),
    (r"\baf\s?1\b", "air force 1"),
    (r"\bnb\b", "new balance"),
    (r"\bdunks\b", "dunk"),
]
# Tokens dropped after punctuation is stripped ("Travis Scott x Jordan 1").
NOISE_TOKENS = {"x", "collab", "collaboration"}

# ---------------------------------------------------------------------------
# Change detection
# ---------------------------------------------------------------------------
# Changes to these fields justify telling the user again.
WATCHED_FIELDS = ["release_datetime", "date_precision", "price", "release_type"]

# A drop missing from its (healthy) source this many runs in a row is flagged
# as possibly cancelled or pulled.
MISSING_RUNS_THRESHOLD = 3