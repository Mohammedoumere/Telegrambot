"""
Telegram Tech News Auto-Poster (website/RSS sources only)
--------------------------------
Reads new articles from RSS feeds you list in RSS_FEEDS
(defaults: Ars Technica, TechCrunch, The Verge).

Keeps only technology-related posts, makes a short summary (no links),
translates it into English / Amharic / Afaan Oromoo, and posts each
language as its own separate message (never mixed) to your target channel.

Includes detailed logging so GitHub Actions logs show real errors
(invalid session, permission issues, entity not found, feed errors, etc.)
"""

import os
import re
import json
import time
import logging
import feedparser
from telethon.sync import TelegramClient
from telethon.sessions import StringSession
from deep_translator import GoogleTranslator

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# ---------- Config from GitHub Secrets (env vars) ----------
API_ID_RAW = os.environ.get("API_ID") or ""
API_HASH = os.environ.get("API_HASH") or ""
SESSION_STRING = os.environ.get("SESSION_STRING") or ""
TARGET_CHANNEL = (os.environ.get("TARGET_CHANNEL") or "").strip()

# Default RSS sources: Ars Technica, TechCrunch, The Verge.
# You can override/extend this via the RSS_FEEDS secret (comma-separated URLs).
DEFAULT_RSS_FEEDS = [
    "http://feeds.bbci.co.uk/news/technology/rss.xml"
    "https://www.aljazeera.com/xml/rss/all.xml",
]
RSS_FEEDS_ENV = os.environ.get("RSS_FEEDS", "").strip()
RSS_FEEDS = [f.strip() for f in RSS_FEEDS_ENV.split(",") if f.strip()] or DEFAULT_RSS_FEEDS
MAX_PER_FEED = int(os.environ.get("MAX_PER_FEED", "3"))

STATE_FILE = "state.json"

TECH_KEYWORDS = [
    # English
    "tech", "technology", "software", "app", "ai", "artificial intelligence",
    "internet", "digital", "startup", "innovation", "cyber", "data",
    "computer", "smartphone", "5g", "telecom", "fintech", "robot",
    "ethio telecom", "safaricom", "e-commerce", "cloud", "app store",
    "google", "microsoft", "meta", "openai", "chatgpt", "gadget",
    # Amharic
    "ቴክኖሎጂ", "ኢኖቬሽን", "ዲጂታል", "ሶፍትዌር", "ኮምፒዩተር", "ኢንተርኔት",
    "ስማርት ስልክ", "አርቴፊሻል ኢንተለጀንስ", "ስታርትአፕ", "አፕሊኬሽን", "ኔትወርክ",
    # Afaan Oromoo
    "teeknooloojii", "invenshinii", "dijitaalaa", "sofuweerii",
    "komputara", "interneetii", "istaartaappii",
]

LANG_LABELS = {
    "en": "🇬🇧 English",
    "am": "🇪🇹 አማርኛ",
    "om": "🟢 Afaan Oromoo",
}


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def clean_text(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)          # strip any HTML tags (RSS summaries have these)
    text = re.sub(r"http\S+|www\.\S+", "", text)   # strip links
    text = re.sub(r"@\w+", "", text)               # strip @mentions
    text = re.sub(r"\s+", " ", text).strip()
    return text


def is_tech(text: str) -> bool:
    lowered = text.lower()
    return any(k in lowered for k in TECH_KEYWORDS)


def summarize(text: str, max_words: int = 45) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words]).rstrip(",.;:") + "…"


def translate(text: str, target: str) -> str:
    if target == "en":
        return text
    try:
        return GoogleTranslator(source="auto", target=target).translate(text)
    except Exception as e:
        logger.warning("Translation to %s failed: %s", target, e)
        return text


def post_summary(client, target_entity, raw_text: str, source_label: str):
    """Post one news item as 3 separate single-language messages."""
    summary_en = summarize(raw_text)
    for lang in ["en", "am", "om"]:
        translated = translate(summary_en, lang)
        post_text = f"{LANG_LABELS[lang]}\n\n{translated}\n\n— {source_label}"
        try:
            client.send_message(target_entity, post_text)
            logger.info("Posted (%s) from %s", lang, source_label)
        except Exception:
            logger.exception("Failed to send message (%s) from %s", lang, source_label)
        time.sleep(3)


def process_rss_feeds(client, target_entity, state):
    for feed_url in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
        except Exception:
            logger.exception("Failed to fetch RSS feed %s", feed_url)
            continue

        source_name = feed.feed.get("title", feed_url) if hasattr(feed, "feed") else feed_url
        seen_key = f"rss:{feed_url}"
        seen_ids = set(state.get(seen_key, []))
        new_seen_ids = list(seen_ids)

        entries = feed.entries[:MAX_PER_FEED]
        entries.reverse()  # oldest first

        for entry in entries:
            entry_id = entry.get("id") or entry.get("link")
            if not entry_id or entry_id in seen_ids:
                continue

            title = entry.get("title", "")
            summary_raw = entry.get("summary", "") or entry.get("description", "")
            raw = clean_text(f"{title}. {summary_raw}")

            if not raw:
                continue
            # Ars Technica / TechCrunch / The Verge are all tech sites already,
            # but keep the keyword check as a light safety net for mixed feeds.
            if not is_tech(raw) and not is_tech(title):
                new_seen_ids.append(entry_id)
                continue

            post_summary(client, target_entity, raw, source_name)
            new_seen_ids.append(entry_id)

        # keep the seen-id list from growing forever
        state[seen_key] = new_seen_ids[-100:]


def main():
    if not API_ID_RAW or not API_HASH or not SESSION_STRING or not TARGET_CHANNEL:
        logger.error(
            "Missing required environment variables. API_ID set: %s, API_HASH set: %s, "
            "SESSION_STRING set: %s, TARGET_CHANNEL set: %s",
            bool(API_ID_RAW), bool(API_HASH), bool(SESSION_STRING), bool(TARGET_CHANNEL),
        )
        raise SystemExit(1)

    if not RSS_FEEDS:
        logger.error("No sources configured: set RSS_FEEDS (or rely on the built-in defaults).")
        raise SystemExit(1)

    api_id = int(API_ID_RAW)
    state = load_state()
    client = TelegramClient(StringSession(SESSION_STRING), api_id, API_HASH)

    with client:
        try:
            logger.info("Resolving target channel: %s", TARGET_CHANNEL)
            target_entity = client.get_entity(TARGET_CHANNEL)
            logger.info("Target resolved: %s", getattr(target_entity, "title", TARGET_CHANNEL))
        except Exception:
            logger.exception("Failed to resolve TARGET_CHANNEL '%s'", TARGET_CHANNEL)
            raise

        logger.info("Checking %d RSS feeds", len(RSS_FEEDS))
        process_rss_feeds(client, target_entity, state)

    save_state(state)
    logger.info("Run complete.")


if __name__ == "__main__":
    main()
