"""Tests for the reliability layer. Run with `pytest`."""
import datetime as dt

import pytest

from reliability import (
    Drop, SchemaError, SeenStore, SourceRef, Tier, commit, dedupe, detect_change,
    drop_key, mark_missing, resolve_ambiguous, score_drop, validate,
)

NOW = dt.datetime(2026, 10, 27, 12, 0, tzinfo=dt.timezone.utc)
PAGE = (
    "The Air Jordan 1 High OG 'Chicago' (DZ5485-612) releases March 14, 2027 "
    "at 10 AM ET on SNKRS for $180."
)

OFFICIAL = SourceRef("nike_snkrs", "https://www.nike.com/launch", trust="retailer_official")
BLOG = SourceRef("sneaker_blog", "https://blog.example.com/a", trust="reputable_blog")
BLOG2 = SourceRef("other_blog", "https://blog2.example.com/b", trust="reputable_blog")
RUMOR_SITE = SourceRef("leaks", "https://leaks.example.com/x", trust="rumor")


def raw(**overrides):
    data = {
        "brand": "Nike", "model": "Air Jordan 1 High OG", "colorway": "Chicago",
        "raw_name": "Air Jordan 1 High OG 'Chicago'", "style_code": "DZ5485-612",
        "release_datetime": "2027-03-14T10:00:00-05:00", "date_precision": "exact_datetime",
        "timezone": "America/New_York", "release_type": "general_release",
        "price": 180, "currency": "USD", "retailer": "Nike SNKRS",
        "url": "https://www.nike.com/launch/air-jordan-1",
        "evidence": "releases March 14, 2027 at 10 AM ET",
        "model_confidence": "high",
    }
    data.update(overrides)
    return data


def make(source=OFFICIAL, **overrides):
    return Drop.from_dict(raw(**overrides), source=source)


# ---------------------------------------------------------------- schema ----
def test_schema_rejects_unknown_field():
    with pytest.raises(SchemaError):
        Drop.from_dict(raw(hype_level=11))


def test_schema_rejects_bad_enum():
    with pytest.raises(SchemaError):
        Drop.from_dict(raw(release_type="sold_out"))


def test_schema_turns_placeholders_into_null():
    drop = Drop.from_dict(raw(price="TBD", colorway="N/A"))
    assert drop.price is None and drop.colorway is None


def test_schema_attaches_timezone_to_naive_datetime():
    drop = Drop.from_dict(raw(release_datetime="2027-03-14T10:00:00"))
    assert drop.release_datetime.tzinfo is not None


# ------------------------------------------------------------ validators ----
def test_valid_drop_passes():
    result = validate(make(), PAGE, NOW)
    assert result.ok, result.errors


def test_past_date_rejected():
    drop = make(release_datetime="2026-01-01T10:00:00-05:00")
    assert "release date is in the past" in validate(drop, PAGE, NOW).errors


def test_evidence_missing_from_page_rejected():
    drop = make(evidence="drops tomorrow at midnight")
    result = validate(drop, PAGE, NOW)
    assert not result.ok
    assert any("evidence" in e for e in result.errors)


def test_evidence_match_ignores_case_and_whitespace():
    drop = make(evidence="RELEASES   March 14, 2027")
    assert validate(drop, PAGE, NOW).ok


def test_url_domain_mismatch_rejected_when_domains_given():
    drop = make(url="https://totally-legit-kicks.biz/jordan")
    result = validate(drop, PAGE, NOW, allowed_domains=["nike.com"])
    assert any("domain" in e for e in result.errors)


def test_subdomain_allowed():
    drop = make(url="https://www.nike.com/x")
    assert validate(drop, PAGE, NOW, allowed_domains=["nike.com"]).ok


def test_bad_url_scheme_rejected():
    drop = make(url="javascript:alert(1)")
    assert not validate(drop, PAGE, NOW).ok


def test_negative_price_warns():
    result = validate(make(price=-5), PAGE, NOW)
    assert any("not positive" in w for w in result.warnings)


def test_price_out_of_range_warns():
    result = validate(make(price=9999), PAGE, NOW)
    assert any("outside" in w for w in result.warnings)


def test_season_precision_with_datetime_rejected():
    drop = make(date_precision="season")
    assert not validate(drop, PAGE, NOW).ok


def test_season_without_datetime_is_ok():
    drop = make(release_datetime=None, date_precision="season", date_text="Spring 2027")
    assert validate(drop, PAGE, NOW).ok


def test_no_timing_rejected():
    drop = make(release_datetime=None, date_precision="unknown")
    assert not validate(drop, PAGE, NOW).ok


def test_bad_style_code_warns():
    result = validate(make(style_code="12"), PAGE, NOW)
    assert any("style code" in w for w in result.warnings)


# ----------------------------------------------------------------- dedupe ---
def test_same_style_code_merges_and_unions_sources():
    result = dedupe([make(OFFICIAL), make(BLOG)])
    assert len(result.merged) == 1
    assert {s.source_id for s in result.merged[0].sources} == {"nike_snkrs", "sneaker_blog"}


def test_style_code_format_differences_still_match():
    result = dedupe([make(OFFICIAL), make(BLOG, style_code="dz5485 612")])
    assert len(result.merged) == 1


def test_different_colorways_do_not_merge():
    result = dedupe([
        make(OFFICIAL, style_code=None, colorway="Chicago"),
        make(BLOG, style_code=None, colorway="Royal"),
    ])
    assert len(result.merged) == 2


def test_alias_names_merge():
    a = make(OFFICIAL, style_code=None, model="Air Jordan 1 High OG")
    b = make(BLOG, style_code=None, model="AJ1 High OG", brand="Nike")
    assert len(dedupe([a, b]).merged) == 1


def test_date_conflict_recorded_not_hidden():
    a = make(OFFICIAL)
    b = make(BLOG, release_datetime="2027-03-21T10:00:00-05:00")
    merged = dedupe([a, b]).merged[0]
    assert len(merged.date_conflicts) == 2
    assert merged.release_datetime.day == 14  # higher-trust source wins


def test_far_apart_dates_stay_separate_restock():
    a = make(OFFICIAL)
    b = make(OFFICIAL, release_datetime="2027-08-01T10:00:00-05:00")
    assert len(dedupe([a, b]).merged) == 2


def test_vague_date_does_not_block_merge_and_exact_wins():
    vague = make(BLOG, release_datetime=None, date_precision="month_only", date_text="March 2027")
    result = dedupe([vague, make(OFFICIAL)])
    assert len(result.merged) == 1
    assert result.merged[0].date_precision.value == "exact_datetime"


def test_ambiguous_pair_returned_not_merged_then_resolved():
    a = make(OFFICIAL, style_code=None, colorway="Chicago")
    b = make(BLOG, style_code=None, colorway=None)
    result = dedupe([a, b])
    assert len(result.merged) == 2 and len(result.ambiguous) == 1
    assert len(resolve_ambiguous(result, ["unsure"]).merged) == 2
    assert len(resolve_ambiguous(result, ["same"]).merged) == 1


def test_dedupe_does_not_mutate_inputs():
    a, b = make(OFFICIAL), make(BLOG)
    dedupe([a, b])
    assert len(a.sources) == 1


# ------------------------------------------------------------- confidence ---
def _score(drop, page=PAGE):
    return score_drop(drop, validate(drop, page, NOW))


def test_official_exact_single_source_is_high():
    result = _score(make(OFFICIAL))
    assert result.tier == Tier.HIGH, result.to_dict()


def test_single_blog_cannot_be_high():
    assert _score(make(BLOG)).tier != Tier.HIGH


def test_two_independent_blogs_reach_high():
    merged = dedupe([make(BLOG), make(BLOG2)]).merged[0]
    assert _score(merged).tier == Tier.HIGH


def test_syndicated_sources_count_once():
    a = SourceRef("site_a", trust="reputable_blog", group="press_wire")
    b = SourceRef("site_b", trust="reputable_blog", group="press_wire")
    merged = dedupe([make(a), make(b)]).merged[0]
    count = next(f for f in _score(merged).factors if f["factor"] == "source_count")
    assert count["value"] == 1


def test_rumor_never_high_even_with_everything_else_perfect():
    drop = make(OFFICIAL, release_type="rumor")
    assert _score(drop).tier != Tier.HIGH


def test_rumor_page_label_caps_tier():
    drop = Drop.from_dict(raw(), source=OFFICIAL, page_label="rumor_or_leak")
    assert _score(drop).tier != Tier.HIGH


def test_month_only_is_capped_at_unconfirmed():
    drop = make(OFFICIAL, release_datetime="2027-03-01", date_precision="month_only",
                evidence="releases March 14, 2027")
    result = _score(drop)
    assert result.tier != Tier.HIGH


def test_date_conflict_lowers_score_and_caps():
    clean = _score(dedupe([make(OFFICIAL), make(BLOG)]).merged[0])
    conflicted = _score(dedupe([make(OFFICIAL),
                                make(BLOG, release_datetime="2027-03-21T10:00:00-05:00")]).merged[0])
    assert conflicted.score < clean.score
    assert conflicted.tier != Tier.HIGH


def test_conflicting_sources_do_not_outscore_a_clean_single_source():
    single = _score(make(OFFICIAL))
    conflicted = _score(dedupe([make(OFFICIAL),
                                make(BLOG, release_datetime="2027-03-21T10:00:00-05:00")]).merged[0])
    assert conflicted.score < single.score


def test_more_sources_raise_score():
    one = _score(make(BLOG))
    two = _score(dedupe([make(BLOG), make(BLOG2)]).merged[0])
    assert two.score > one.score


def test_failed_validation_is_log_only():
    drop = make(evidence="not on the page")
    result = _score(drop)
    assert result.tier == Tier.LOG_ONLY and result.factors[0]["decisive"]


def test_scoring_is_deterministic():
    drop = make(BLOG)
    assert _score(drop).to_dict() == _score(drop).to_dict()


def test_every_factor_has_a_note_and_decisive_sort_first():
    result = _score(make(OFFICIAL, release_type="rumor"))
    assert all(f["note"] for f in result.factors)
    flags = [f["decisive"] for f in result.factors]
    assert flags == sorted(flags, reverse=True)


def test_rumor_example_from_a_single_blog_is_log_only():
    drop = make(BLOG, release_type="rumor", release_datetime=None,
                date_precision="month_only", date_text="March 2027",
                evidence="releases March 14, 2027")
    assert _score(drop).tier == Tier.LOG_ONLY


# ---------------------------------------------------------------- changes ---
def test_new_drop_is_notify_worthy():
    store = SeenStore()
    change = detect_change(make(), store, Tier.HIGH)
    assert change.status == "new" and change.notify_worthy


def test_log_only_never_notify_worthy():
    change = detect_change(make(), SeenStore(), Tier.LOG_ONLY)
    assert not change.notify_worthy


def test_unchanged_after_notification():
    store, drop = SeenStore(), make()
    commit(drop, store, NOW, Tier.HIGH, notified=True)
    assert detect_change(drop, store, Tier.HIGH).status == "unchanged"


def test_failed_notification_is_retried():
    store, drop = SeenStore(), make()
    commit(drop, store, NOW, Tier.HIGH, notified=False)
    assert detect_change(drop, store, Tier.HIGH).status == "new"


def test_date_change_triggers_update_with_old_and_new():
    store = SeenStore()
    commit(make(), store, NOW, Tier.HIGH, notified=True)
    moved = make(release_datetime="2027-03-21T10:00:00-05:00")
    change = detect_change(moved, store, Tier.HIGH)
    assert change.status == "updated" and change.notify_worthy
    info = change.to_change_info()["changes"][0]
    assert info["field"] == "release_datetime"
    assert info["previous_value"].startswith("2027-03-14")
    assert info["new_value"].startswith("2027-03-21")


def test_name_rewording_is_not_a_change():
    store = SeenStore()
    commit(make(), store, NOW, Tier.HIGH, notified=True)
    reworded = make(raw_name="AJ1 High Chicago (2027 Reimagined)")
    assert detect_change(reworded, store, Tier.HIGH).status == "unchanged"


def test_tier_upgrade_is_worth_notifying():
    store, drop = SeenStore(), make()
    commit(drop, store, NOW, Tier.UNCONFIRMED, notified=True)
    change = detect_change(drop, store, Tier.HIGH)
    assert change.status == "updated"
    assert change.changes[0].field == "tier"


def test_tier_downgrade_is_silent():
    store, drop = SeenStore(), make()
    commit(drop, store, NOW, Tier.HIGH, notified=True)
    assert detect_change(drop, store, Tier.UNCONFIRMED).status == "unchanged"


def test_drop_key_ignores_date_but_not_identity():
    assert drop_key(make()) == drop_key(make(release_datetime="2027-04-01T10:00:00-05:00"))
    assert drop_key(make()) != drop_key(make(style_code="DD1391-100"))


def test_missing_drop_flagged_once_after_threshold():
    store, drop = SeenStore(), make()
    key = commit(drop, store, NOW, Tier.HIGH, notified=True)
    flagged = []
    for _ in range(3):
        flagged += mark_missing(store, [], ["nike_snkrs"], NOW)
    assert flagged == [key]
    assert mark_missing(store, [], ["nike_snkrs"], NOW) == []  # not flagged twice


def test_missing_not_counted_when_source_was_down():
    store = SeenStore()
    commit(make(), store, NOW, Tier.HIGH, notified=True)
    for _ in range(5):
        assert mark_missing(store, [], ["some_other_source"], NOW) == []


def test_seen_again_resets_missing_counter():
    store, drop = SeenStore(), make()
    key = commit(drop, store, NOW, Tier.HIGH, notified=True)
    mark_missing(store, [], ["nike_snkrs"], NOW)
    commit(drop, store, NOW, Tier.HIGH, notified=False)
    assert store.get(key)["missing_runs"] == 0


def test_store_round_trips_to_disk(tmp_path):
    path = str(tmp_path / "seen.json")
    store, drop = SeenStore(path), make()
    commit(drop, store, NOW, Tier.HIGH, notified=True)
    store.save()
    reloaded = SeenStore(path)
    assert detect_change(drop, reloaded, Tier.HIGH).status == "unchanged"