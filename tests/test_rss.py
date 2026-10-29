from unittest.mock import patch

from tools.rss import fetch_feed

SAMPLE = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Sneaker News</title>
<item><title>Jordan 4 drops Saturday</title><link>https://example.com/a</link>
<pubDate>Mon, 26 Oct 2026 10:00:00 GMT</pubDate><description>Kith, $210</description></item>
<item><title>Dunk Low restock</title><link>https://example.com/b</link></item>
</channel></rss>"""


def test_fetch_feed_parses_entries():
    with patch("tools.rss._download", return_value=SAMPLE):
        result = fetch_feed("https://example.com/feed")
    assert result["title"] == "Sneaker News"
    assert len(result["entries"]) == 2
    assert result["entries"][0]["title"] == "Jordan 4 drops Saturday"
    assert result["entries"][1]["published"] == ""


def test_fetch_feed_respects_limit():
    with patch("tools.rss._download", return_value=SAMPLE):
        assert len(fetch_feed("https://example.com/feed", limit=1)["entries"]) == 1


def test_fetch_feed_returns_error_on_network_failure():
    with patch("tools.rss._download", side_effect=OSError("boom")):
        result = fetch_feed("https://example.com/feed")
    assert "boom" in result["error"] and "entries" not in result


def test_fetch_feed_returns_error_on_garbage():
    with patch("tools.rss._download", return_value=b"not a feed"):
        assert "error" in fetch_feed("https://example.com/feed")


def test_fetch_all_feeds_combines_sources_and_reports_failures():
    from tools.rss import fetch_all_feeds

    feeds = [{"name": "A", "url": "https://a.test/feed"}, {"name": "B", "url": "https://b.test/feed"}]

    def fake_download(url, timeout):
        if "b.test" in url:
            raise OSError("down")
        return SAMPLE

    with patch("tools.sources.FEEDS", feeds), patch("tools.rss._download", side_effect=fake_download):
        result = fetch_all_feeds()
    assert [e["source"] for e in result["entries"]] == ["A", "A"]
    assert result["errors"][0]["source"] == "B"
