import datetime as dt
import json
from unittest.mock import patch

from agent import SneakerHunterAgent

# Relative to today so the tests do not age into "release is in the past".
DATE = (dt.date.today() + dt.timedelta(days=30)).isoformat()
LATER_DATE = (dt.date.today() + dt.timedelta(days=45)).isoformat()

ENTRIES = [
    {"title": f"Air Jordan 14 Forest Green releases {DATE}", "link": "https://a.test/1",
     "published": "", "summary": "<p>Retail $210 at Nike.</p>", "source": "Nice Kicks", "trust": "reputable_blog"},
    {"title": "Best sneakers of the year", "link": "https://a.test/2",
     "published": "", "summary": "A roundup.", "source": "Highsnobiety", "trust": "reputable_blog"},
]


class FakeClient:
    """Classifies by title and returns one drop for the Jordan entry."""

    def complete(self, system_prompt, user_prompt, json_mode=True):
        if "write short notifications" in system_prompt:
            return f"Jordan Air Jordan 14 Forest Green releases {DATE} at Nike."
        if "explain confidence ratings" in system_prompt:
            return json.dumps({"summary": "Unconfirmed: one source.", "reasons": ["One blog reported it."], "to_improve": None})
        if "page classifier" in system_prompt:
            upcoming = "Jordan" in user_prompt
            return json.dumps({"label": "drop_announcement" if upcoming else "editorial_or_roundup",
                               "contains_upcoming_release": upcoming, "certainty": "high",
                               "reason": "x", "evidence": None})
        return json.dumps({"drops": [{
            "brand": "Jordan", "model": "Air Jordan 14", "colorway": "Forest Green",
            "release_datetime": DATE, "date_precision": "date_only", "release_type": "general_release",
            "price": 210, "currency": "USD", "retailer": "Nike", "url": "https://a.test/1",
            "evidence": f"Air Jordan 14 Forest Green releases {DATE}"}]})


def _run(client, **kwargs):
    with patch("agent.fetch_all_feeds", return_value={"entries": ENTRIES, "errors": []}):
        return SneakerHunterAgent(client=client, **kwargs).run()


def test_offline_mode_returns_entries_without_drops():
    result = _run(None)
    assert len(result["entries"]) == 2
    assert result["drops"] == []
    assert {"entries", "drops", "rejected", "errors", "logs"} <= set(result)


def test_workflow_extracts_only_upcoming_release():
    result = _run(FakeClient())
    assert len(result["drops"]) == 1
    drop = result["drops"][0]
    assert drop["drop"]["model"] == "Air Jordan 14"
    assert drop["drop"]["sources"][0]["source_id"] == "Nice Kicks"
    assert "tier" in drop["confidence"]


def test_bad_llm_output_is_logged_not_raised():
    class Garbage:
        def complete(self, system_prompt, user_prompt):
            return "not json"

    result = _run(Garbage())
    assert result["drops"] == []
    assert any("not valid JSON" in log["message"] for log in result["logs"])


def test_api_error_is_logged_not_raised():
    class Boom:
        def complete(self, system_prompt, user_prompt):
            raise RuntimeError("rate limited")

    result = _run(Boom())
    assert result["drops"] == []
    assert any("rate limited" in log["message"] for log in result["logs"])


def test_call_budget_is_enforced_and_covers_every_source():
    calls = []

    class Counting(FakeClient):
        def complete(self, system_prompt, user_prompt, json_mode=True):
            calls.append(1)
            return super().complete(system_prompt, user_prompt, json_mode)

    entries = [
        {**ENTRIES[1], "title": f"A{i}", "source": "A"} for i in range(5)
    ] + [{**ENTRIES[1], "title": f"B{i}", "source": "B"} for i in range(5)]
    with patch("agent.fetch_all_feeds", return_value={"entries": entries, "errors": []}):
        agent = SneakerHunterAgent(client=Counting(), max_calls=4)
        result = agent.run()

    assert len(calls) == 4 and result["calls_used"] == 4
    assert any("budget" in log["message"] for log in result["logs"])
    # interleaved: first four classified entries are A0, B0, A1, B1
    assert [e["source"] for e in agent._interleave(entries)][:4] == ["A", "B", "A", "B"]


def test_new_drop_gets_a_summary_and_is_remembered():
    from reliability import SeenStore

    store = SeenStore()
    first = _run(FakeClient(), store=store)["drops"][0]
    assert first["change"]["status"] == "new"
    assert first["message"].startswith("Jordan")

    second = _run(FakeClient(), store=store)["drops"][0]
    assert second["change"]["status"] == "unchanged"
    assert second["message"] is None


def test_failed_summary_is_retried_next_run():
    from reliability import SeenStore

    class NoSummary(FakeClient):
        def complete(self, system_prompt, user_prompt, json_mode=True):
            if "write short notifications" in system_prompt:
                raise RuntimeError("rate limited")
            return super().complete(system_prompt, user_prompt, json_mode)

    store = SeenStore()
    failed = _run(NoSummary(), store=store)["drops"][0]
    assert failed["message"] is None and failed["notify_worthy"]

    retry = _run(FakeClient(), store=store)["drops"][0]
    assert retry["change"]["status"] == "new" and retry["message"]


def test_changed_date_is_announced_as_an_update():
    from reliability import SeenStore

    store = SeenStore()
    _run(FakeClient(), store=store)

    class MovedDate(FakeClient):
        def complete(self, system_prompt, user_prompt, json_mode=True):
            raw = super().complete(system_prompt, user_prompt, json_mode)
            return raw.replace(DATE, LATER_DATE) if "drops" in raw else raw

    entries = [{**ENTRIES[0], "title": ENTRIES[0]["title"].replace(DATE, LATER_DATE)}]
    with patch("agent.fetch_all_feeds", return_value={"entries": entries, "errors": []}):
        drop = SneakerHunterAgent(client=MovedDate(), store=store).run()["drops"][0]
    assert drop["change"]["status"] == "updated"
    assert drop["change"]["info"]["changes"][0]["field"] == "release_datetime"


def test_explanation_is_off_by_default_and_on_when_requested():
    assert _run(FakeClient())["drops"][0]["explanation"] is None
    explained = _run(FakeClient(), explain=True)["drops"][0]["explanation"]
    assert explained["reasons"] == ["One blog reported it."]


def test_summary_uses_plain_text_mode_and_extraction_uses_json_mode():
    modes = {}

    class Recording(FakeClient):
        def complete(self, system_prompt, user_prompt, json_mode=True):
            modes["summary" if "write short notifications" in system_prompt else "other"] = json_mode
            return super().complete(system_prompt, user_prompt, json_mode)

    _run(Recording())
    assert modes == {"summary": False, "other": True}


def test_scoring_does_not_penalize_missing_page_text():
    factors = _run(FakeClient())["drops"][0]["confidence"]["factors"]
    assert not any("page text not available" in f["note"] for f in factors)


class _Counting(FakeClient):
    def __init__(self):
        self.calls = 0

    def complete(self, system_prompt, user_prompt, json_mode=True):
        self.calls += 1
        return super().complete(system_prompt, user_prompt, json_mode)


def test_already_analyzed_entries_cost_no_calls_on_the_next_run():
    from reliability import SeenStore

    log, store = SeenStore(), SeenStore()
    first = _Counting()
    _run(first, entry_log=log, store=store)
    assert first.calls > 0

    second = _Counting()
    result = _run(second, entry_log=log, store=store)
    assert second.calls == 0
    assert [s["reason"] for s in result["skipped"]].count("already analyzed") == 1


def test_title_filter_skips_non_drops_without_a_call_and_reports_them():
    client = _Counting()
    result = _run(client)
    # ENTRIES[1] is "Best sneakers of the year": skipped. Only the Jordan entry is analyzed
    # (classify + extract + summarize).
    assert client.calls == 3
    assert [s["title"] for s in result["skipped"]] == ["Best sneakers of the year"]
    assert result["skipped"][0]["reason"] == "title looks like a non-drop"


def test_title_filter_is_narrow_and_keeps_real_drop_titles():
    from agent import NON_DROP_TITLE

    keep = [
        "Reese’s Serves Up A Friends And Family Reebok Angel Reese 1",
        "Air Jordan 14 “Forest Green” (487471-300) October 2026 Release Date & Info",
        "Nike Celebrates World Series Champion Milwaukee Brewers With An Air Force 1 Release",
        "Fragment Design X Nike Air Liquid Max Black / Anthracite",
    ]
    skip = ["Air Jordan 4 Review: Worth It?", "The 10 Best Sneakers Of 2026", "How To Style Dunks"]
    assert not any(NON_DROP_TITLE.search(t) for t in keep)
    assert all(NON_DROP_TITLE.search(t) for t in skip)


def test_failed_calls_leave_entries_unmarked_so_they_are_retried():
    from reliability import SeenStore

    class Garbage:
        def complete(self, system_prompt, user_prompt, json_mode=True):
            return "not json"

    log = SeenStore()
    _run(Garbage(), entry_log=log)
    assert log.records == {}

    ok = _Counting()
    _run(ok, entry_log=log)
    assert ok.calls > 0


def test_entries_cut_off_by_the_budget_are_analyzed_next_run():
    from reliability import SeenStore

    log = SeenStore()
    entries = [{**ENTRIES[0], "title": f"Jordan {i} release", "link": f"https://a.test/{i}", "source": "A"} for i in range(4)]

    def run(max_calls):
        with patch("agent.fetch_all_feeds", return_value={"entries": entries, "errors": []}):
            return SneakerHunterAgent(client=FakeClient(), max_calls=max_calls, entry_log=log).run()

    run(4)  # classify + extract + summarize per entry: only a couple of entries fit
    done_first = len(log.records)
    assert 0 < done_first < 4
    run(20)
    assert len(log.records) == 4
