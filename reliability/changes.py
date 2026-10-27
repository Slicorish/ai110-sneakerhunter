"""Detect what changed since the user was last told about a drop.

Comparison is against what the user was LAST NOTIFIED about
(`notified_values`), not against the last run. That way a failed notification
is retried next run instead of being silently lost.

Typical use in agent.py:

    change = detect_change(drop, store, tier)
    if change.notify_worthy:
        ok = notifier.send(...)             # may fail
    commit(drop, store, now, tier, notified=change.notify_worthy and ok)
    ...
    store.save()                            # once, after the loop
"""
from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
from dataclasses import dataclass, field
from typing import Iterable, Optional

from . import config
from .consolidate import normalize_style_code, normalize_text
from .schema import Drop, Tier, as_aware


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------
class SeenStore:
    """Tiny JSON-backed store. Pass path=None for an in-memory store (tests)."""

    def __init__(self, path: Optional[str] = None):
        self.path = path
        self.records: dict = {}
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                self.records = json.load(fh).get("records", {})

    def get(self, key: str) -> Optional[dict]:
        return self.records.get(key)

    def put(self, key: str, record: dict) -> None:
        self.records[key] = record

    def save(self) -> None:
        """Atomic write so a crash cannot leave a half-written file."""
        if not self.path:
            return
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "records": self.records}, fh, indent=2, sort_keys=True)
        os.replace(tmp_path, self.path)


# ---------------------------------------------------------------------------
# Keys and snapshots
# ---------------------------------------------------------------------------
def drop_key(drop: Drop) -> str:
    """Stable identity for a drop. Deliberately excludes the date, so a date
    change shows up as an update to the same drop, not as a new one."""
    code = normalize_style_code(drop.style_code)
    if code:
        return f"sc:{code}"
    parts = [normalize_text(drop.brand), normalize_text(drop.model), normalize_text(drop.colorway)]
    if not any(parts):
        parts = [normalize_text(drop.raw_name)]
    return "nm:" + "|".join(parts)


def snapshot(drop: Drop) -> dict:
    """The watched fields, in JSON-safe form."""
    values = {
        "release_datetime": drop.release_datetime.isoformat() if drop.release_datetime else None,
        "date_precision": drop.date_precision.value,
        "price": drop.price,
        "release_type": drop.release_type.value,
    }
    return {name: values[name] for name in config.WATCHED_FIELDS}


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------
@dataclass
class FieldChange:
    field: str
    previous: object
    new: object


@dataclass
class ChangeResult:
    key: str
    status: str                                   # "new" | "updated" | "unchanged"
    changes: list = field(default_factory=list)   # list[FieldChange]
    notify_worthy: bool = False

    def to_change_info(self) -> Optional[dict]:
        """Shape expected by summarize_drop.txt's {{change_info}}; None unless updated."""
        if self.status != "updated":
            return None
        return {
            "status": "updated",
            "changes": [
                {"field": c.field, "previous_value": c.previous, "new_value": c.new}
                for c in self.changes
            ],
        }


def detect_change(drop: Drop, store: SeenStore, tier: Tier) -> ChangeResult:
    """Compare a drop against what the user was last told."""
    key = drop_key(drop)
    record = store.get(key)
    worthy_tier = tier != Tier.LOG_ONLY

    if record is None or "notified_values" not in record:
        return ChangeResult(key=key, status="new", notify_worthy=worthy_tier)

    current = snapshot(drop)
    previous = record["notified_values"]
    changes = [
        FieldChange(name, previous.get(name), current[name])
        for name in config.WATCHED_FIELDS
        if previous.get(name) != current[name]
    ]

    # Moving up a tier (e.g. unconfirmed -> high) is worth telling the user.
    last_tier = Tier(record.get("notified_tier", "log_only"))
    if worthy_tier and tier.rank > last_tier.rank:
        changes.append(FieldChange("tier", last_tier.value, tier.value))

    if not changes:
        return ChangeResult(key=key, status="unchanged")
    return ChangeResult(key=key, status="updated", changes=changes, notify_worthy=worthy_tier)


def commit(drop: Drop, store: SeenStore, now: dt.datetime, tier: Tier, notified: bool) -> str:
    """Record that this drop was seen this run, and optionally that the user was told.

    Call with notified=True only after the notification actually succeeded.
    Does not write to disk; call store.save() once after the run.
    """
    key = drop_key(drop)
    record = store.get(key) or {"first_seen": now.isoformat()}
    values = snapshot(drop)
    record.update({
        "display_name": drop.display_name,
        "last_seen": now.isoformat(),
        "missing_runs": 0,
        "vanished": False,
        "values": values,
        "last_tier": tier.value,
        "source_ids": sorted({s.source_id for s in drop.sources}),
    })
    if notified:
        record["notified_values"] = values
        record["notified_tier"] = tier.value
    store.put(key, record)
    return key


def mark_missing(
    store: SeenStore,
    seen_keys: Iterable[str],
    healthy_source_ids: Iterable[str],
    now: dt.datetime,
) -> list:
    """Flag upcoming drops that keep disappearing from sources that loaded fine.

    `healthy_source_ids` must only contain sources fetched successfully this
    run, otherwise an outage would look like a wave of cancellations.
    Drops with no concrete date are skipped: we cannot tell "pulled" from "released".
    Returns keys newly flagged as possibly cancelled (each is flagged once).
    """
    now = as_aware(now)
    seen, healthy = set(seen_keys), set(healthy_source_ids)
    flagged = []
    for key, record in store.records.items():
        if key in seen or record.get("vanished"):
            continue
        if not healthy.intersection(record.get("source_ids", [])):
            continue
        release = record.get("values", {}).get("release_datetime")
        if not release or as_aware(dt.datetime.fromisoformat(release)) < now:
            continue
        record["missing_runs"] = record.get("missing_runs", 0) + 1
        if record["missing_runs"] >= config.MISSING_RUNS_THRESHOLD:
            record["vanished"] = True
            flagged.append(key)
    return flagged