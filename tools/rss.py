import ssl
import urllib.request
from typing import Any, Dict, List

import certifi
import feedparser

USER_AGENT = "sneakerhunter/0.1 (+rss reader)"


def _download(url: str, timeout: float) -> bytes:
    context = ssl.create_default_context(cafile=certifi.where())
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
        body = response.read()
        # Bot-protected sites answer with 202 and an empty body instead of an error status.
        if response.status != 200 or not body:
            raise RuntimeError(f"HTTP {response.status} with {len(body)} bytes (blocked or empty)")
        return body


def fetch_feed(url: str, limit: int = 20, timeout: float = 10.0) -> Dict[str, Any]:
    """
    Fetch an RSS or Atom feed and return its most recent entries.

    Args:
        url: Feed URL, e.g. a sneaker news site's RSS link.
        limit: Maximum number of entries to return.
        timeout: Network timeout in seconds.

    Returns:
        {"source": url, "title": feed title, "entries": [...]} on success, where each
        entry has title, link, published, summary. On failure, {"source": url, "error": "..."}
        so the caller can handle it without a crash.
    """
    try:
        parsed = feedparser.parse(_download(url, timeout))
    except Exception as exc:
        return {"source": url, "error": f"fetch failed: {exc}"}

    # feedparser sets `bozo` on malformed feeds; only fail if nothing usable came out.
    if parsed.bozo and not parsed.entries:
        return {"source": url, "error": f"could not parse feed: {parsed.bozo_exception}"}

    entries: List[Dict[str, str]] = [
        {
            "title": entry.get("title", ""),
            "link": entry.get("link", ""),
            "published": entry.get("published", entry.get("updated", "")),
            "summary": entry.get("summary", ""),
        }
        for entry in parsed.entries[:limit]
    ]
    return {"source": url, "title": parsed.feed.get("title", ""), "entries": entries}


def fetch_all_feeds(limit_per_feed: int = 10) -> Dict[str, Any]:
    """
    Fetch every feed in tools.sources.FEEDS and combine the entries.

    Args:
        limit_per_feed: Maximum entries to take from each feed.

    Returns:
        {"entries": [...], "errors": [...]}. Each entry also carries a "source" name.
        A feed that fails is listed in errors; the others still come back.
    """
    from tools.sources import FEEDS

    entries: List[Dict[str, str]] = []
    errors: List[Dict[str, str]] = []
    for feed in FEEDS:
        result = fetch_feed(feed["url"], limit=limit_per_feed)
        if "error" in result:
            errors.append({"source": feed["name"], "error": result["error"]})
            continue
        entries.extend(
            {**entry, "source": feed["name"], "trust": feed.get("trust", "aggregator")}
            for entry in result["entries"]
        )
    return {"entries": entries, "errors": errors}
