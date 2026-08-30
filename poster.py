"""
Ethiopian & World News Auto-Poster (RSS feeds + Telegram channels)
--------------------------------
Reads new articles from:
  1) RSS feeds you list in RSS_FEEDS (world/tech/Ethiopian outlets that have feeds)
  2) Telegram channels you list in SOURCE_CHANNELS (for outlets with no RSS,
     e.g. Jille Communication, OMN, MinT, Ethiopian Press Agency)

Posts ALL news (no topic filter), makes a short summary (no links),
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

# RSS sources: general news, all topics (not tech-only).
DEFAULT_RSS_FEEDS = [
    "https://techcrunch.com/feed/",
    "http://feeds.bbci.co.uk/news/technology/rss.xml",
    "https://www.aljazeera.com/xml/rss/all.xml",
    "https://amharic.voanews.com/api/zt$gteitjt",
    "https://addisstandard.com/feed/",
    "https://ethsat.com/feed/",
    "https://capitalethiopia.com/feed/",
    "https://waltainfo.com/feed/",
]
RSS_FEEDS_ENV = os.environ.get("RSS_FEEDS", "").strip()
RSS_FEEDS = [f.strip() for f in RSS_FEEDS_ENV.split(",") if f.strip()] or DEFAULT_RSS_FEEDS
MAX_PER_FEED = int(os.environ.get("MAX_PER_FEED", "1"))

# Telegram channel sources: outlets with no RSS feed available.
DEFAULT_SOURCE_CHANNELS = [
    "jille_com",             # Jille Timuga Government Communication
    "oromiamedianetworks",   # OMN - Oromia Media Network
    "MinTEthiopia",          # Ministry of Innovation and Technology
    "ethpress",              # Ethiopian Press Agency
]
SOURCE_CHANNELS_ENV = os.environ.get("SOURCE_CHANNELS", "").strip()
SOURCE_CHANNELS = [c.strip() for c in SOURCE_CHANNELS_ENV.split(",") if c.strip()] or DEFAULT_SOURCE_CHANNELS
MAX_PER_CHANNEL = int(os.environ.get("MAX_PER_CHANNEL", "1"))

STATE_FILE = "state.json"

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
    text = re.sub(r"<[^>]+>", " ", text)          # strip any HTML tags
    text = re.sub(r"http\S+|www\.\S+", "", text)   # strip links
    text = re.sub(r"@\w+", "", text)               # strip @mentions
    text = re.sub(r"\s+", " ", text).strip()
    return text


def summarize(text: str, max_words: int = 45) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words]).rstrip(",.;:") + "…"


def translate(text: str, target: str):
    """Translate text. Returns the translated string, or None if translation failed."""
    if target == "en":
        return text
    try:
        result = GoogleTranslator(source="auto", target=target).translate(text)
        if not result or not result.strip():
            logger.warning("Translation to %s returned empty result", target)
            return None
        return result
    except Exception as e:
        logger.warning("Translation to %s failed: %s", target, e)
        return None


def post_summary(client, target_entity, raw_text: str, source_label: str):
    """Post one news item as up to 3 separate single-language messages, one at a time.
    If a language's translation fails or looks wrong, that language is skipped
    (not posted) rather than posting broken/untranslated text."""
    summary_en = summarize(raw_text)
    for lang in ["en", "am", "om"]:
        translated = translate(summary_en, lang)

        if translated is None:
            logger.warning("Skipping post (%s) from %s: translation failed", lang, source_label)
            continue

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
        entries.reverse()  # oldest first, chronological posting

        for entry in entries:
            entry_id = entry.get("id") or entry.get("link")
            if not entry_id or entry_id in seen_ids:
                continue

            title = entry.get("title", "")
            summary_raw = entry.get("summary", "") or entry.get("description", "")
            raw = clean_text(f"{title}. {summary_raw}")

            if not raw:
                new_seen_ids.append(entry_id)
                continue

            post_summary(client, target_entity, raw, source_name)
            new_seen_ids.append(entry_id)

        state[seen_key] = new_seen_ids[-100:]


def process_telegram_channels(client, target_entity, state):
    for channel in SOURCE_CHANNELS:
        last_id = state.get(f"tg:{channel}", 0)
        newest_id = last_id
        try:
            messages = list(client.iter_messages(channel, limit=MAX_PER_CHANNEL))
        except Exception:
            logger.exception(
                "Failed to fetch messages from %s (make sure your account has joined this channel)",
                channel,
            )
            continue

        messages.reverse()  # oldest first, chronological posting

        for msg in messages:
            if not getattr(msg, "message", None):
                continue
            if msg.id <= last_id:
                continue
            newest_id = max(newest_id, msg.id)

            raw = clean_text(msg.message)
            if not raw:
                continue

            post_summary(client, target_entity, raw, channel)

        state[f"tg:{channel}"] = newest_id


def main():
    if not API_ID_RAW or not API_HASH or not SESSION_STRING or not TARGET_CHANNEL:
        logger.error(
            "Missing required environment variables. API_ID set: %s, API_HASH set: %s, "
            "SESSION_STRING set: %s, TARGET_CHANNEL set: %s",
            bool(API_ID_RAW), bool(API_HASH), bool(SESSION_STRING), bool(TARGET_CHANNEL),
        )
        raise SystemExit(1)

    if not RSS_FEEDS and not SOURCE_CHANNELS:
        logger.error("No sources configured: set RSS_FEEDS and/or SOURCE_CHANNELS.")
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

        if RSS_FEEDS:
            logger.info("Checking %d RSS feeds", len(RSS_FEEDS))
            process_rss_feeds(client, target_entity, state)

        if SOURCE_CHANNELS:
            logger.info("Checking %d Telegram source channels", len(SOURCE_CHANNELS))
            process_telegram_channels(client, target_entity, state)

    save_state(state)
    logger.info("Run complete.")


if __name__ == "__main__":
    main()
