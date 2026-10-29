# Sneaker Hunter Model Card (Reflection)

Sections marked **TODO** need your own observations from running the app in Gemini mode. Everything else describes how the system is built.

---

## 1) What is this system?

**Name:** Sneaker Hunter
**Purpose:** Read a curated set of sneaker news RSS feeds, find posts announcing upcoming releases, extract structured drop details, and rate how trustworthy each drop is.

**Intended users:** Sneaker buyers who want a short, ranked list of upcoming drops with an honest confidence rating, rather than reading every blog.

**Not intended for:** Automated purchasing, or treating a rating as a guarantee. A `high` rating means several signals agree, not that the release is certain.

---

## 2) How does it work?

Pipeline: **fetch → classify → extract → validate → consolidate and score**.

| Step | Done by | Notes |
| --- | --- | --- |
| Fetch feeds | Code (`feedparser`) | 4 feeds in `tools/sources.py`; failures are skipped and reported |
| Classify entry | Gemini | Label plus whether an upcoming release is mentioned |
| Extract drops | Gemini | Structured JSON; unstated fields must be null |
| Validate | Code | Evidence must appear in the entry text; checks dates, price, style code, URL |
| Dedupe | Code | Merges the same drop across sources, records date conflicts |
| Track changes | Code | Compares each drop with what the user was last told (`SeenStore`) |
| Score confidence | Code | Rule-based; the LLM never sets the rating |
| Summarize, explain | Gemini | Short notification for new or changed drops; optional plain-language explanation of the rating. Both only restate fields computed by code |

Without an API key the app only fetches and lists entries; there is no heuristic extractor.

---

## 3) Inputs and outputs

**Inputs:** The title and summary of each RSS entry. The full article page is **not** fetched, so details that appear only in the article body can be missed.

**Outputs:** For each drop: name, date and date precision, release type, price, retailer, source links, the supporting quote, a confidence score and tier, and the reasons behind the rating. Rejected drops and failed feeds are listed separately.

**Sources and why they were chosen:** Only feeds that allow automated readers. Hypebeast returns an empty bot-challenge page (HTTP 202) and Kith returns HTTP 429 on every request, so both were left out rather than bypassed.

---

## 4) Reliability and safety rules

Examples of rules in `reliability/`:

1. **Evidence check** (`validators.py`): the quoted evidence must appear in the entry text.
   - Why it matters: it catches invented dates or details the model hallucinated.
   - False positive: the model paraphrases instead of quoting exactly, so a correct drop is rejected.
   - False negative: the quote is real but the model attached it to the wrong claim.
2. **Source trust and independence** (`confidence.py`, `config.py`): a drop reported by one blog scores lower than one confirmed by several independent sources.
   - Why it matters: one blog can be wrong or repeating a rumor.
   - False positive: blogs that copy each other count as agreeing. Sources in the same `group` count as one to limit this, but the feeds here are not yet grouped.
   - False negative: a real drop only one outlet has broken is rated low.
3. **Date precision penalty**: "Spring 2027" or "March" scores below an exact date and is never turned into a specific day.
4. **Announce once** (`changes.py`): a drop is summarized only when it is new or a watched field changed, and counts as announced only if the summary was actually produced, so a failed call is retried.
5. **LLM call budget** (`agent.py`): the agent stops after a set number of Gemini calls to protect the free tier.
   - False negative: entries beyond the budget are never analyzed, so real drops can be missed. The UI warns when this happens.

---

## 5) Observed failure modes

**TODO:** add at least two real examples from your runs. Ideas to check:

1. A real drop that was missed, for example because the date was only in the article body and not the summary.
2. A post classified as an upcoming drop that was really a recap, rumor, or roundup.
3. The same release extracted twice because the names differed between sources and dedupe did not merge them.

Known limits already observed while building:

* Highsnobiety was removed as a source: it covers general fashion, so most entries were not sneaker drops and would waste the call budget.
* `mark_missing` (flagging drops that vanish from a source as possibly cancelled) is deliberately not used. Blog posts scroll off an RSS feed after a few days, so it would report real drops as cancelled.
* Feed dates use different formats and `published` is the post date, not the release date.

---

## 6) Feeds-only vs Gemini comparison

**TODO:** run both modes and describe the difference. Feeds-only mode lists entries without analysis, so the comparison is really: how many of the listed entries did Gemini turn into drops, how many of those passed validation, and did the confidence ratings match your own judgment?

---

## 7) Human-in-the-loop decision

The system never acts on a drop; it only reports. The human decision is whether to trust a rating.

* `unconfirmed` and `log_only` drops should be treated as leads to check, not facts.
* **TODO:** describe one scenario where the tool should refuse to rate a drop, for example a single source, a month-only date, and a rumor label together, and where you would implement that rule.

---

## 8) Improvement idea

**TODO:** pick one. Candidates:

* Fetch the full article page for entries classified as drops, so dates and prices in the body are captured.
* Put sources that copy each other in the same `group` so they count as one independent source.
* Add a retailer product-page check (for example Shopify `products.json` on stores that allow it) to confirm a drop exists.
