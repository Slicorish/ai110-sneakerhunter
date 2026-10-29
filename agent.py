import datetime as dt
import html
import json
import os
import re
from itertools import zip_longest
from typing import Any, Dict, List, Optional

from reliability import (
    Drop,
    SchemaError,
    SeenStore,
    SourceRef,
    commit,
    dedupe,
    detect_change,
    score_drop,
    validate,
)
from tools import fetch_all_feeds

# Directory where prompt templates live, relative to this file
PROMPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")

# Longest notification the summarize prompt is asked to write.
SUMMARY_MAX_LENGTH = 280

# Titles containing these are clearly not drop announcements, so they are skipped without an
# LLM call. Kept deliberately narrow: a missed non-drop only costs one call, but a wrongly
# skipped drop is lost. Every skipped title is reported so the list can be checked.
NON_DROP_TITLE = re.compile(
    r"\b(review|reviewed|best|top \d+|ranked|ranking|guide|how to|history|interview|opinion|"
    r"unboxing|on-foot review|worth it|vs\.?|recap|explained)\b",
    re.IGNORECASE,
)


def _load_prompt(filename: str) -> str:
    """Load a prompt template from the prompts/ folder."""
    path = os.path.join(PROMPTS_DIR, filename)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


class SneakerHunterAgent:
    """
    Sneaker Hunter runs a small agentic workflow over a curated set of RSS feeds:

    1) FETCH: pull recent entries from every feed in tools/sources.py
    2) CLASSIFY: ask the LLM whether each entry announces an upcoming release
    3) EXTRACT: ask the LLM to pull structured drop details from those entries
    4) VALIDATE: check each drop against the entry text it came from
    5) CONSOLIDATE: merge the same drop reported by several sources, then score confidence
    6) TRACK: compare each drop with what the user was last told (SeenStore)
    7) SUMMARIZE: write a short notification for drops that are new or changed
       (optionally also a plain-language explanation of the confidence rating)
    """

    def __init__(
        self,
        client: Optional[Any] = None,
        limit_per_feed: int = 10,
        max_calls: Optional[int] = None,
        store: Optional[SeenStore] = None,
        explain: bool = False,
        entry_log: Optional[SeenStore] = None,
    ):
        # client should implement: complete(system_prompt: str, user_prompt: str) -> str
        # (summaries also pass json_mode=False; clients that accept it get plain text).
        # max_calls caps LLM requests per run (e.g. to stay inside a free tier). None means no cap.
        # store remembers what the user was told; the default is in-memory, so nothing persists.
        # explain adds one LLM call per announced drop to explain its confidence rating.
        # entry_log remembers which feed entries were already analyzed, so repeat runs only
        # spend calls on new posts. The default is in-memory, so nothing persists.
        self.client = client
        self.limit_per_feed = limit_per_feed
        self.max_calls = max_calls
        self.store = store if store is not None else SeenStore()
        self.explain = explain
        self.entry_log = entry_log if entry_log is not None else SeenStore()
        self.calls_used = 0
        self.logs: List[Dict[str, str]] = []

    # ----------------------------
    # Public API
    # ----------------------------
    def run(self) -> Dict[str, Any]:
        self.logs = []
        self.calls_used = 0
        now = dt.datetime.now(dt.timezone.utc)
        self._log("PLAN", "Reading curated feeds, then classify -> extract -> validate -> consolidate -> track -> summarize.")

        feed_result = self.fetch()
        entries = feed_result["entries"]
        for err in feed_result["errors"]:
            self._log("FETCH", f"{err['source']} failed: {err['error']}")
        self._log("FETCH", f"Got {len(entries)} entries.")

        if not self._can_call_llm():
            self._log("CLASSIFY", "No LLM client configured. Returning entries without extraction.")
            return {
                "entries": entries,
                "skipped": [],
                "drops": [],
                "rejected": [],
                "errors": feed_result["errors"],
                "calls_used": 0,
                "logs": self.logs,
            }

        drops: List[Drop] = []
        rejected: List[Dict[str, Any]] = []
        page_texts: Dict[str, str] = {}  # entry link -> the text drops from it were checked against
        to_process, skipped = self._select_new_entries(entries)
        self._log(
            "FILTER",
            f"{len(to_process)} entries to analyze; skipped {len(skipped)} "
            f"({sum(1 for s in skipped if s['reason'] == 'already analyzed')} already analyzed, "
            f"{sum(1 for s in skipped if s['reason'] != 'already analyzed')} not drop-like).",
        )
        for entry in self._interleave(to_process):
            if self._budget_spent():
                self._log("CLASSIFY", f"API call budget ({self.max_calls}) used up. Remaining entries were not analyzed.")
                break
            page_text = self._entry_text(entry)
            label = self.classify(entry, page_text, now)
            if label is None:
                continue  # the call failed; leave the entry unmarked so it is retried next run
            self._log(
                "CLASSIFY",
                f"'{entry.get('title', '')[:50]}' -> {label.get('label')} "
                f"(upcoming={label.get('contains_upcoming_release')}, certainty={label.get('certainty')})",
            )
            if not label.get("contains_upcoming_release"):
                self._mark_analyzed(entry, "not_upcoming", now)
                continue
            found = self.extract(entry, page_text, label.get("label"), now)
            if found is None:
                continue  # extraction failed; retry next run
            self._log("EXTRACT", f"'{entry.get('title', '')[:50]}' -> {len(found)} drop(s) extracted.")
            self._mark_analyzed(entry, f"extracted_{len(found)}", now)
            for drop in found:
                validation = validate(drop, page_text, now)
                if not validation.ok:
                    rejected.append({"drop": drop.to_dict(), "errors": validation.errors})
                    self._log("VALIDATE", f"Rejected '{drop.display_name}': {'; '.join(validation.errors)}")
                    continue
                page_texts[entry.get("link", "")] = page_text
                drops.append(drop)
        self._log("EXTRACT", f"{len(drops)} drop(s) passed validation, {len(rejected)} rejected.")

        merged = dedupe(drops).merged
        self._log("CONSOLIDATE", f"{len(drops)} drop(s) merged into {len(merged)}.")

        scored = []
        for drop in merged:
            # Re-validate against the text of every entry that reported this drop, so the
            # evidence check still runs and no "page text not available" penalty is applied.
            text = " ".join(page_texts.get(src.url or "", "") for src in drop.sources).strip()
            validation = validate(drop, text or None, now)
            scored.append((drop, score_drop(drop, validation)))
        # Most trustworthy first, so a small call budget is spent on the best drops.
        scored.sort(key=lambda pair: pair[1].score, reverse=True)

        results = []
        for drop, confidence in scored:
            change = detect_change(drop, self.store, confidence.tier)
            message = explanation = None
            if change.notify_worthy:
                message = self.summarize(drop, confidence, change)
                if self.explain:
                    explanation = self.explain_confidence(drop, confidence)
            # Count the user as told only if a message was produced, so a skipped or
            # failed call leaves the drop "new" and it is announced again next run.
            commit(drop, self.store, now, confidence.tier, notified=message is not None)
            results.append(
                {
                    "drop": drop.to_dict(),
                    "confidence": confidence.to_dict(),
                    "change": {"status": change.status, "info": change.to_change_info()},
                    "notify_worthy": change.notify_worthy,
                    "message": message,
                    "explanation": explanation,
                }
            )
        self.store.save()
        self.entry_log.save()
        announced = sum(1 for r in results if r["message"])
        self._log("TRACK", f"{announced} of {len(results)} drop(s) announced; the rest are unchanged or log-only.")

        return {
            "entries": entries,
            "skipped": skipped,
            "drops": results,
            "rejected": rejected,
            "errors": feed_result["errors"],
            "calls_used": self.calls_used,
            "logs": self.logs,
        }

    # ----------------------------
    # Workflow steps
    # ----------------------------
    def fetch(self) -> Dict[str, Any]:
        return fetch_all_feeds(limit_per_feed=self.limit_per_feed)

    def classify(self, entry: Dict[str, str], page_text: str, now: dt.datetime) -> Optional[Dict[str, Any]]:
        system_prompt = self._fill("classify_page.txt", entry, now)
        parsed = self._ask(system_prompt, self._wrap(page_text), "CLASSIFY", entry.get("title", ""))
        if not isinstance(parsed, dict):
            return None
        return parsed

    def extract(
        self, entry: Dict[str, str], page_text: str, label: Optional[str], now: dt.datetime
    ) -> Optional[List[Drop]]:
        """Drops found in an entry. None means the call failed or was unreadable (retry later);
        an empty list means the model answered and found none."""
        system_prompt = self._fill("extract_drop.txt", entry, now)
        parsed = self._ask(system_prompt, self._wrap(page_text), "EXTRACT", entry.get("title", ""))
        if not isinstance(parsed, dict) or not isinstance(parsed.get("drops"), list):
            return None

        source = SourceRef(source_id=entry["source"], url=entry.get("link"), trust=entry.get("trust", "aggregator"))
        drops: List[Drop] = []
        for item in parsed["drops"]:
            try:
                drops.append(Drop.from_dict(item, source=source, page_label=label))
            except SchemaError as e:
                self._log("EXTRACT", f"Skipped a malformed drop from {entry['source']}: {e}")
        return drops

    # ----------------------------
    # Parsing + utilities
    # ----------------------------
    def _fill(self, prompt_file: str, entry: Dict[str, str], now: dt.datetime) -> str:
        return (
            _load_prompt(prompt_file)
            .replace("{{current_date}}", now.date().isoformat())
            .replace("{{source_url}}", entry.get("link", ""))
        )

    def summarize(self, drop: Drop, confidence: Any, change: Any) -> Optional[str]:
        """Write a short notification for a new or changed drop, or None if the call failed."""
        reason = " ".join(f"{f['note']}." for f in confidence.factors[:2])
        # drop_json is replaced last: it holds model-extracted text, which must not be
        # scanned for further {{placeholders}}.
        system_prompt = (
            _load_prompt("summarize_drop.txt")
            .replace("{{channel}}", "app")
            .replace("{{max_length}}", str(SUMMARY_MAX_LENGTH))
            .replace("{{confidence_tier}}", confidence.tier.value)
            .replace("{{confidence_reason}}", reason)
            .replace("{{change_info}}", json.dumps(change.to_change_info()))
            .replace("{{drop_json}}", json.dumps(drop.to_dict()))
        )
        raw = self._call(system_prompt, "Write the notification.", "SUMMARIZE", drop.display_name, json_mode=False)
        return raw.strip() if raw else None

    def explain_confidence(self, drop: Drop, confidence: Any) -> Optional[Dict[str, Any]]:
        """Explain a rating in plain language. The rating itself is never changed."""
        system_prompt = (
            _load_prompt("explain_confidence.txt")
            .replace("{{drop_name}}", drop.display_name)
            .replace("{{tier}}", confidence.tier.value)
            .replace("{{score}}", str(confidence.score))
        )
        factors = f"<factors>\n{json.dumps(confidence.factors)}\n</factors>"
        parsed = self._ask(system_prompt, factors, "EXPLAIN", drop.display_name)
        if not isinstance(parsed, dict) or not isinstance(parsed.get("reasons"), list) or not parsed.get("summary"):
            return None
        return parsed

    def _call(self, system_prompt: str, user_prompt: str, step: str, label: str, **kwargs: Any) -> Optional[str]:
        """One budgeted LLM request. Returns the raw text, or None if skipped or failed."""
        if self._budget_spent():
            self._log(step, "API call budget used up. Skipping this call.")
            return None
        self.calls_used += 1
        try:
            raw = self.client.complete(system_prompt=system_prompt, user_prompt=user_prompt, **kwargs)
        except Exception as e:
            self._log(step, f"API Error on '{label[:50]}': {e}")
            return None
        if not raw.strip():
            self._log(step, f"Empty LLM response for '{label[:50]}'.")
            return None
        return raw

    def _ask(self, system_prompt: str, user_prompt: str, step: str, label: str) -> Any:
        """Like _call, but parses the response as JSON."""
        raw = self._call(system_prompt, user_prompt, step, label)
        if raw is None:
            return None
        parsed = self._parse_json(raw)
        if parsed is None:
            self._log(step, f"LLM output was not valid JSON for '{label[:50]}'.")
        return parsed

    def _wrap(self, page_text: str) -> str:
        return f"<page_content>\n{page_text}\n</page_content>"

    def _entry_text(self, entry: Dict[str, str]) -> str:
        summary = html.unescape(re.sub(r"<[^>]+>", " ", entry.get("summary", "")))
        return re.sub(r"\s+", " ", f"{entry.get('title', '')}. {summary}").strip()

    def _parse_json(self, text: str) -> Any:
        text = text.strip()
        match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
        if match:
            text = match.group(1)
        try:
            return json.loads(text)
        except Exception:
            return None

    def _select_new_entries(self, entries: List[Dict[str, str]]):
        """Split entries into those worth an LLM call and those skipped (with the reason)."""
        to_process: List[Dict[str, str]] = []
        skipped: List[Dict[str, str]] = []
        for entry in entries:
            if self.entry_log.get(entry.get("link", "")) is not None:
                skipped.append({"title": entry.get("title", ""), "source": entry["source"], "reason": "already analyzed"})
            elif NON_DROP_TITLE.search(entry.get("title", "")):
                skipped.append({"title": entry.get("title", ""), "source": entry["source"], "reason": "title looks like a non-drop"})
            else:
                to_process.append(entry)
        return to_process, skipped

    def _mark_analyzed(self, entry: Dict[str, str], outcome: str, now: dt.datetime) -> None:
        link = entry.get("link", "")
        if link:
            self.entry_log.put(link, {"title": entry.get("title", ""), "outcome": outcome, "analyzed": now.isoformat()})

    def _budget_spent(self) -> bool:
        return self.max_calls is not None and self.calls_used >= self.max_calls

    def _interleave(self, entries: List[Dict[str, str]]) -> List[Dict[str, str]]:
        """Round-robin entries across sources so a small call budget still covers every feed."""
        by_source: Dict[str, List[Dict[str, str]]] = {}
        for entry in entries:
            by_source.setdefault(entry["source"], []).append(entry)
        ordered: List[Dict[str, str]] = []
        for round_entries in zip_longest(*by_source.values()):
            ordered.extend(e for e in round_entries if e is not None)
        return ordered

    def _can_call_llm(self) -> bool:
        return self.client is not None and hasattr(self.client, "complete")

    def _log(self, step: str, message: str) -> None:
        self.logs.append({"step": step, "message": message})
