# 👟 Sneaker Hunter

Sneaker Hunter is a small, agent-style tracker for upcoming sneaker releases. It reads a curated set of sneaker news RSS feeds, finds posts that announce an upcoming drop, extracts the details, and rates how much to trust each one before showing it to you.

---

## What Sneaker Hunter Does

1. **Fetches** recent entries from the feeds listed in `tools/sources.py`
   - Sneaker News, Nice Kicks, KicksOnFire
   - A feed that fails is reported and skipped; the rest still load

2. **Classifies** each entry (Gemini)
   - Is this an upcoming drop, a raffle, a rumor, a roundup, a recap, or irrelevant?
   - Only entries that mention an upcoming release continue

3. **Extracts** structured drop details (Gemini)
   - Brand, model, colorway, style code, date and how precise it is, price, retailer, release type
   - Fields the text does not state stay empty rather than guessed

4. **Validates** each drop with plain code, no LLM
   - Checks identity, timing, price, style code, and that the quoted evidence really appears in the entry text

5. **Consolidates and scores** (no LLM)
   - The same drop reported by several sources is merged
   - Each drop gets a confidence score and tier: `high`, `unconfirmed`, or `log_only`
   - The score depends on source trust, how many independent sources agree, date precision, release type, and more

6. **Tracks what changed** (no LLM)
   - Remembers which drops you were already told about, in `data/seen_drops.json` (gitignored)
   - A drop is marked new, updated (date, price, precision, or type changed, or its tier went up), or already reported

7. **Summarizes** (Gemini)
   - Writes a short notification for each new or updated drop that is not `log_only`
   - Optionally explains the confidence rating in plain language (the rating itself is always computed by code)
   - If a summary call fails or the budget runs out, the drop stays "new" and is announced again next run

8. **Shows its work**
   - Ranked drop cards with the reasons behind each rating
   - Drops that failed validation, feeds that failed, and a step-by-step agent trace

---

## Project Layout

```text
app.py                 Streamlit UI
agent.py               SneakerHunterAgent: fetch -> classify -> extract -> validate -> consolidate
llm_client.py          Gemini wrapper
tools/
  rss.py               fetch_feed(url), fetch_all_feeds()
  sources.py           The curated feed list, with a trust level for each
prompts/               Prompt templates for classify, extract, summarize, explain
data/                  Local memory of announced drops (created on first run, gitignored)
reliability/           Schema, validators, dedupe, confidence scoring, change tracking
tests/                 Agent and RSS tests
```

To add or remove a source, edit `tools/sources.py`. Only add feeds that allow automated readers. Hypebeast, for example, answers bots with a challenge page and returns nothing, so it is left out.

---

## Setup

### 1. Create a virtual environment (recommended)

```bash
python -m venv .venv
source .venv/bin/activate   # macOS/Linux
# or
.venv\Scripts\activate      # Windows
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

---

## Running without an API key

```bash
streamlit run app.py
```

In the sidebar, select **Mode: Feeds only (no API)**, then click **Find drops**. This fetches and lists the latest feed entries but does not analyze them.

---

## Running with Gemini

### 1. Set up your API key

```bash
cp .env.example .env
```

Edit `.env` and add your Gemini API key:

```text
GEMINI_API_KEY=your_real_key_here
```

`.env` is gitignored. Never put a real key in `.env.example`, which is tracked.

### 2. Run the app

```bash
streamlit run app.py
```

In the sidebar, select **Mode: Gemini (requires API key)**, then choose the model, entries per feed, and **Max Gemini calls per run**.

**Free tier note:** the free tier allows about 20 requests. Each entry costs one call to classify, plus one to extract if it announces a release. Each announced drop costs one more call for its summary, and another if you turn on plain-language explanations. The call cap is a hard limit: the agent stops calling Gemini once it is used up, and entries are read round-robin across feeds so every source gets coverage. Calls used are shown after each run.

---

## Running Tests

Tests cover the RSS tools, the agent workflow (with a fake LLM, so no API calls), and the reliability layer.

```bash
pytest
```

You should see tests covering:

* Feed parsing, limits, and failure handling
* The agent workflow, including bad LLM output, API errors, and the call budget
* Validation, dedupe, confidence scoring, and change detection
