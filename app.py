import os
import re

import streamlit as st
from dotenv import load_dotenv

from agent import SneakerHunterAgent
from llm_client import GeminiClient
from reliability import SeenStore
from tools.sources import FEEDS

# ----------------------------
# App setup
# ----------------------------
st.set_page_config(page_title="Sneaker Hunter", page_icon="👟", layout="wide")
st.title("👟 Sneaker Hunter")
st.caption("Reads sneaker news feeds, finds upcoming drops, and rates how much to trust each one.")

# Load environment variables from .env if present
load_dotenv()

def safe(text) -> str:
    """Escape Markdown so text from feeds or the model is shown literally.

    Streamlit treats `$...$` as math and `:name[...]` as formatting, so an unescaped
    price like "$210" or a stray bracket would be eaten or restyled.
    """
    return re.sub(r"([\\`*_{}\[\]()<>#+\-.!|~$:&])", r"\\\1", str(text))


def safe_url(url) -> str:
    """Make a URL safe inside a Markdown link."""
    return str(url).replace(" ", "%20").replace("(", "%28").replace(")", "%29")


TIER_LABELS = {"high": "HIGH", "unconfirmed": "UNCONFIRMED", "log_only": "LOG ONLY"}
STATUS_LABELS = {"new": "🆕 New", "updated": "🔄 Updated", "unchanged": "✔️ Already reported"}

# Remembers which drops the user has been told about, so repeat runs only announce what is new or changed.
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
STORE_PATH = os.path.join(DATA_DIR, "seen_drops.json")
# Remembers which feed entries were already analyzed, so repeat runs only spend calls on new posts.
ENTRY_LOG_PATH = os.path.join(DATA_DIR, "analyzed_entries.json")

# ----------------------------
# Sidebar controls
# ----------------------------
st.sidebar.header("Settings")

mode = st.sidebar.selectbox(
    "Mode",
    ["Feeds only (no API)", "Gemini (requires API key)"],
    help="Feeds only fetches and lists entries. Gemini mode also classifies entries and extracts drops.",
)

limit_per_feed = st.sidebar.slider(
    "Entries per feed",
    min_value=1,
    max_value=15,
    value=3,
    help="How many recent entries to read from each feed.",
)

max_calls = st.sidebar.slider(
    "Max Gemini calls per run",
    min_value=1,
    max_value=20,
    value=10,
    disabled=(mode != "Gemini (requires API key)"),
    help="Hard cap. The agent stops calling Gemini once this is used up. Each entry costs 1 call to classify, plus 1 to extract if it announces a release.",
)

if mode == "Gemini (requires API key)":
    st.sidebar.warning(
        f"⚠️ Gemini Free Tier: ~20 requests. This run will stop after {max_calls} calls, "
        "so some entries may not be analyzed. Use Feeds only mode to save your quota."
    )

model_name = st.sidebar.selectbox(
    "Gemini model",
    ["gemini-flash-lite-latest", "gemini-2.5-flash", "gemini-2.5-pro"],
    disabled=(mode != "Gemini (requires API key)"),
)

temperature = st.sidebar.slider(
    "Temperature",
    min_value=0.0,
    max_value=1.0,
    value=0.2,
    step=0.1,
    disabled=(mode != "Gemini (requires API key)"),
    help="Lower values tend to be more consistent.",
)

st.sidebar.divider()

remember = st.sidebar.checkbox(
    "Remember drops between runs",
    value=True,
    help="When on, entries already analyzed are skipped (saving Gemini calls), and drops you were already told about are marked 'Already reported' and not summarized again.",
)
explain = st.sidebar.checkbox(
    "Explain ratings in plain language",
    value=False,
    disabled=(mode != "Gemini (requires API key)"),
    help="Costs 1 extra Gemini call per announced drop. The factor list under each drop is always shown.",
)
if st.sidebar.button(
    "Forget remembered drops",
    disabled=not (os.path.exists(STORE_PATH) or os.path.exists(ENTRY_LOG_PATH)),
):
    for path in (STORE_PATH, ENTRY_LOG_PATH):
        if os.path.exists(path):
            os.remove(path)
    st.sidebar.success("Memory cleared. Every entry will be analyzed again.")

st.sidebar.divider()
st.sidebar.write("**Sources**")
for feed in FEEDS:
    st.sidebar.write(f"- {feed['name']}")

show_debug = st.sidebar.checkbox("Show debug details", value=False)

# ----------------------------
# Choose client
# ----------------------------
client = None
if mode == "Feeds only (no API)":
    client_status = "No LLM. Entries are fetched but not analyzed."
else:
    if not os.getenv("GEMINI_API_KEY", "").strip():
        client_status = "Missing GEMINI_API_KEY. Add it to your .env file to use Gemini mode."
    else:
        client = GeminiClient(model_name=model_name, temperature=temperature)
        client_status = "Gemini client ready."

st.sidebar.info(client_status)

# ----------------------------
# Run workflow
# ----------------------------
run_button = st.button("Find drops", type="primary", use_container_width=True)

if not run_button:
    st.write("Run the workflow to see upcoming drops, confidence ratings, and the agent trace.")
    st.stop()

if mode == "Gemini (requires API key)" and client is None:
    st.error("Gemini mode is selected, but no API key is available.")
    st.stop()

agent = SneakerHunterAgent(
    client=client,
    limit_per_feed=limit_per_feed,
    max_calls=max_calls if client is not None else None,
    store=SeenStore(STORE_PATH) if remember else None,
    entry_log=SeenStore(ENTRY_LOG_PATH) if remember else None,
    explain=explain and client is not None,
)

with st.spinner("Hunting through the feeds..."):
    result = agent.run()

drops = result.get("drops", [])
rejected = result.get("rejected", [])
entries = result.get("entries", [])
skipped = result.get("skipped", [])
errors = result.get("errors", [])
logs = result.get("logs", [])

for err in errors:
    st.warning(safe(f"Could not read {err['source']}: {err['error']}"))

if client is not None:
    st.caption(f"Gemini calls used: {result.get('calls_used', 0)} of {max_calls}")

if any("budget" in log.get("message", "") for log in logs):
    st.warning("Call budget reached: some entries were not analyzed. Raise the cap or lower entries per feed.")

if any("API Error" in log.get("message", "") for log in logs):
    st.warning("⚠️ API Request Failed: some entries were skipped because of a limit or network error.")

# ----------------------------
# Drops
# ----------------------------
st.subheader("Upcoming drops")

if client is None:
    st.info("Feeds only mode: no drops were extracted. Switch to Gemini mode to analyze the entries below.")
elif not drops:
    st.info("No upcoming drops were found in these entries.")

for item in drops:
    drop = item["drop"]
    confidence = item["confidence"]
    tier = confidence["tier"]

    name = safe(" ".join(p for p in (drop["brand"], drop["model"], drop["colorway"]) if p) or drop["raw_name"] or "(unnamed drop)")
    with st.container(border=True):
        head_left, head_right = st.columns([3, 1])
        with head_left:
            st.markdown(f"### {name}")
            st.caption(STATUS_LABELS.get(item["change"]["status"], ""))
        with head_right:
            st.metric(TIER_LABELS.get(tier, tier), f"{confidence['score']:.2f}")

        if item["message"]:
            st.info(safe(item["message"]))
        elif item["notify_worthy"]:
            st.caption("No summary was written (call budget reached or the call failed). It will be announced again next run.")
        if item["explanation"]:
            exp = item["explanation"]
            st.markdown(f"**{safe(exp['summary'])}**")
            for reason in exp["reasons"]:
                st.write(f"- {safe(reason)}")
            if exp.get("to_improve"):
                st.caption(f"To raise confidence: {safe(exp['to_improve'])}")
        if item["change"]["info"]:
            for ch in item["change"]["info"]["changes"]:
                st.caption(safe(f"Changed {ch['field']}: {ch['previous_value']} → {ch['new_value']}"))

        when = drop["release_datetime"] or drop["date_text"] or "Date unknown"
        details = [f"**When:** {safe(when)} ({safe(drop['date_precision'])})", f"**Type:** {safe(drop['release_type'])}"]
        if drop["price"] is not None:
            details.append(f"**Price:** {safe(drop['price'])} {safe(drop['currency'] or '')}".strip())
        if drop["retailer"]:
            details.append(f"**Retailer:** {safe(drop['retailer'])}")
        st.write("  \n".join(details))

        reported_by = ", ".join(s["source_id"] for s in drop["sources"])
        st.caption(f"Reported by: {safe(reported_by)}")
        for src in drop["sources"]:
            if src["url"]:
                st.markdown(f"[{safe(src['source_id'])}]({safe_url(src['url'])})")

        if drop["evidence"]:
            st.caption(f"Evidence: “{safe(drop['evidence'])}”")

        with st.expander("Why this rating"):
            for factor in confidence["factors"]:
                st.write(f"- **{safe(factor['factor'])}** ({safe(factor['status'])}): {safe(factor['note'])}")

if rejected:
    with st.expander(f"{len(rejected)} drop(s) rejected by validation"):
        for item in rejected:
            drop = item["drop"]
            label = drop["raw_name"] or drop["model"] or "(unnamed drop)"
            st.write(f"**{safe(label)}**: {safe('; '.join(item['errors']))}")

st.divider()

# ----------------------------
# Entries
# ----------------------------
st.subheader(f"Feed entries ({len(entries)})")
for entry in entries:
    st.markdown(f"- [{safe(entry['title'])}]({safe_url(entry['link'])}) — {safe(entry['source'])}")

if skipped:
    with st.expander(f"{len(skipped)} entr{'y' if len(skipped) == 1 else 'ies'} skipped without a Gemini call"):
        st.caption("Check this list to make sure no real drop was skipped by the title filter.")
        for item in skipped:
            st.write(f"- {safe(item['title'])} — {safe(item['source'])} ({safe(item['reason'])})")

st.divider()

# ----------------------------
# Trace
# ----------------------------
st.subheader("Agent trace")
if not logs:
    st.info("No trace logs were produced.")
else:
    for entry in logs:
        st.write(f"**{safe(entry.get('step', 'LOG'))}:** {safe(entry.get('message', ''))}")

if show_debug:
    st.divider()
    st.subheader("Debug payload")
    st.json(result)
