"""
Ethiopian & World News Auto-Poster (RSS feeds + Telegram channels)
--------------------------------
Reads new articles from:
  1) RSS feeds you list in RSS_FEEDS (world/tech/Ethiopian outlets that have feeds)
  2) Telegram channels you list in SOURCE_CHANNELS (for outlets with no RSS,
     e.g. Jille Communication, OMN, MinT, Ethiopian Press Agency)

Posts news with:
  - Clear, attractive headlines
  - Longer, engaging summaries (200 words max, context-aware)
  - Multi-language translations (English / Amharic / Afaan Oromoo)
  - Each language as separate message (never mixed)
  - News category tags when available
  - Source attribution

Includes detailed logging for GitHub Actions
(invalid session, permission issues, entity not found, feed errors, etc.)
"""

import os
import re
import json
import time
import hashlib
import logging
import requests
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
VERBOSE = os.environ.get("VERBOSE", "").lower() in ("1", "true", "yes")
if VERBOSE:
    logger.setLevel(logging.DEBUG)

# RSS sources: general news, all topics (not tech-only).
DEFAULT_RSS_FEEDS = [
    "http://feeds.bbci.co.uk/news/rss.xml",
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

# Optional: OpenAI API key (ChatGPT-based translation, tried if Google's isn't set or fails).
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

LANG_NAMES = {
    "en": "English",
    "am": "Amharic",
    "om": "Afaan Oromo (Oromo)",
}

STATE_FILE = "state.json"

LANG_LABELS = {
    "en": "🇬🇧 English",
    "am": "🇪🇹 አማርኛ",
    "om": "🟢 Afaan Oromoo",
}

# News category emojis
CATEGORY_EMOJIS = {
    "politics": "🏛️",
    "election": "🗳️",
    "government": "🏛️",
    "business": "💼",
    "economy": "📊",
    "finance": "💰",
    "market": "📈",
    "tech": "💻",
    "technology": "🖥️",
    "science": "🔬",
    "health": "🏥",
    "medical": "⚕️",
    "sport": "⚽",
    "sports": "🏆",
    "entertainment": "🎬",
    "culture": "🎭",
    "weather": "🌤️",
    "climate": "🌍",
    "environment": "🌱",
    "war": "⚠️",
    "conflict": "⚠️",
    "security": "🛡️",
    "crime": "🚨",
    "education": "📚",
    "agriculture": "🌾",
    "energy": "⚡",
}

# Category keywords with detailed context needs
DETAILED_CATEGORIES = {
    "politics": ["politics", "election", "government", "parliament", "vote", "bill", "law"],
    "business": ["business", "company", "market", "investment", "stock", "entrepreneur"],
    "health": ["health", "medical", "disease", "vaccine", "hospital", "doctor"],
    "tech": ["tech", "technology", "software", "ai", "app", "digital", "internet"],
    "science": ["science", "research", "study", "discovery", "experiment", "scientist"],
    "sport": ["sport", "team", "player", "game", "match", "championship", "league"],
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
    """Remove HTML tags, links, mentions, and excess whitespace."""
    text = re.sub(r"<[^>]+>", " ", text)          # strip any HTML tags
    text = re.sub(r"http\S+|www\.\S+", "", text)   # strip links
    text = re.sub(r"@\w+", "", text)               # strip @mentions
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_headline(title: str, max_chars: int = 120) -> str:
    """Extract and clean a compelling headline."""
    headline = clean_text(title).strip()
    if len(headline) > max_chars:
        # Try to cut at a word boundary
        truncated = headline[:max_chars]
        last_space = truncated.rfind(" ")
        if last_space > max_chars * 0.7:
            headline = truncated[:last_space].rstrip(",.;:") + "…"
        else:
            headline = truncated.rstrip(",.;:") + "…"
    return headline


def detect_category(title: str, summary: str) -> str:
    """Detect news category based on keywords in title and summary."""
    combined = (title + " " + summary).lower()
    for category, keywords in DETAILED_CATEGORIES.items():
        for keyword in keywords:
            if keyword in combined:
                return category
    return "general"


def guess_category(title: str, summary: str) -> str:
    """Guess news category from title and summary to select an emoji."""
    combined = (title + " " + summary).lower()
    for keyword, emoji in CATEGORY_EMOJIS.items():
        if keyword in combined:
            return emoji
    return "📰"  # default news emoji


def summarize(text: str, title: str = "", max_words: int = 200) -> str:
    """Create an intelligent, context-aware summary.
    
    Base length is 200 words, but adapts based on category:
    - Politics/Business/Health: Full context (200 words) for important details
    - Tech/Science: Full length for explanations
    - Sports: Full length for game details
    - General: Standard 200 words
    """
    text = text.strip()
    words = text.split()
    
    # Detect category to determine summary depth
    category = detect_category(title, text)
    
    # Adjust word count based on category importance
    if category in ["politics", "business", "health"]:
        word_limit = 200  # Full context for important news
    elif category in ["tech", "science"]:
        word_limit = 200  # Detailed explanations needed
    elif category == "sport":
        word_limit = 190   # Game details
    else:
        word_limit = 200  # Default for general news
    
    if len(words) <= word_limit:
        return text
    
    # Take first N words and try to end at sentence or phrase boundary
    summary = " ".join(words[:word_limit]).rstrip(",.;:") + "…"
    return summary


def translate_via_openai(text: str, target: str):
    """Use ChatGPT (OpenAI) for translation. Returns translated text or None on failure."""
    lang_name = LANG_NAMES.get(target, target)
    try:
        resp = requests.post(
            OPENAI_URL,
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": OPENAI_MODEL,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            f"You are a professional news translator. Translate the user's text into "
                            f"{lang_name}. Output ONLY the translated text, with no notes, quotes, "
                            f"labels, or explanations."
                        ),
                    },
                    {"role": "user", "content": text},
                ],
                "temperature": 0.2,
            },
            timeout=15,
        )
        if resp.status_code != 200:
            logger.warning("OpenAI API error %s: %s", resp.status_code, resp.text[:200])
            return None
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.warning("OpenAI API request failed: %s", e)
        return None


def translate(text: str, target: str):
    """Translate text using, in order: ChatGPT/OpenAI (if configured),
    then the free unofficial Google Translate method as a last resort.
    Returns the translated string, or None if every method failed
    or came back looking like an error page."""
    if target == "en":
        return text

    result = None
    
    # Try OpenAI first if configured
    if OPENAI_API_KEY:
        result = translate_via_openai(text, target)

    # Fall back to Google Translate if OpenAI didn't work
    if result is None:
        try:
            result = GoogleTranslator(source="auto", target=target).translate(text)
        except Exception as e:
            logger.warning("Translation to %s failed: %s", target, e)
            return None

    # Validate the result
    if not result or not result.strip():
        logger.warning("Translation to %s returned empty result", target)
        return None

    if not is_valid_translation(result):
        logger.warning("Translation to %s looked like an error page, discarding: %r", target, result[:120])
        return None

    return result


ERROR_PAGE_SIGNS = [
    "error 500", "server error", "that's an error", "that's an error",
    "that's all we know", "that's all we know", "404 not found",
    "bad gateway", "service unavailable", "please try again later",
    "<html", "<!doctype",
]


def is_valid_translation(text: str) -> bool:
    """Reject text that looks like an HTML/server error page instead of a real translation."""
    lowered = text.lower()
    return not any(sign in lowered for sign in ERROR_PAGE_SIGNS)


def make_dedup_key(text: str) -> str:
    """Normalize text so the same story from different sources hashes the same way."""
    normalized = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
    normalized = " ".join(normalized.split()[:12])  # first ~12 words is enough to catch dupes
    return hashlib.md5(normalized.encode("utf-8")).hexdigest()


def post_summary(client, target_entity, raw_text: str, source_label: str, 
                 title: str = "", image_url: str = "") -> bool:
    """Post one news item as up to 3 separate single-language messages, one per minute.
    
    Format:
    🇬🇧 English
    📰 HEADLINE
    
    Full context summary (200 words, category-aware)...
    
    — Source Name
    
    If a language's translation fails, that language is skipped.
    Returns True if at least one language was successfully posted.
    """
    summary_en = summarize(raw_text, title)
    headline = extract_headline(title) if title else None
    category_emoji = guess_category(title or "", raw_text)
    
    posted_any = False
    for lang in ["en", "am", "om"]:
        translated_summary = translate(summary_en, lang)
        if translated_summary is None:
            logger.warning("Skipping post (%s) from %s: translation failed", lang, source_label)
            continue

        translated_headline = None
        if headline:
            translated_headline = translate(headline, lang)
            if translated_headline is None:
                translated_headline = headline  # fall back to original

        # Build attractive message format
        parts = [LANG_LABELS[lang]]
        
        if translated_headline:
            parts.append(f"{category_emoji} {translated_headline}")
        
        parts.append(f"\n{translated_summary}")
        parts.append(f"\n— {source_label}")
        
        post_text = "\n".join(parts)

        try:
            if image_url:
                try:
                    logger.debug("Sending file to %r (image_url=%s)", getattr(target_entity, 'username', target_entity), image_url)
                    client.send_file(target_entity, image_url, caption=post_text)
                    logger.info("Posted (%s) with image from %s", lang, source_label)
                    posted_any = True
                except Exception as e:
                    logger.warning("Image send failed for %s, falling back to text-only: %s", source_label, e)
                    try:
                        client.send_message(target_entity, post_text)
                        logger.info("Posted (%s) text-only from %s", lang, source_label)
                        posted_any = True
                    except Exception as e2:
                        logger.exception("Failed to send text message (%s) from %s: %s", lang, source_label, e2)
            else:
                logger.debug("Sending message to %r", getattr(target_entity, 'username', target_entity))
                client.send_message(target_entity, post_text)
                logger.info("Posted (%s) from %s", lang, source_label)
                posted_any = True
        except Exception as e:
            logger.exception("Failed to send message (%s) from %s: %s", lang, source_label, e)

        # Sleep between languages to avoid flood limits; can be configured via env var
        sleep_between = int(os.environ.get("SLEEP_BETWEEN_LANGS", "60"))
        if sleep_between > 0:
            time.sleep(sleep_between)

    return posted_any


def extract_image_url(entry) -> str:
    """Try several common RSS fields to find an article image."""
    try:
        if hasattr(entry, "media_content") and entry.media_content:
            url = entry.media_content[0].get("url")
            if url:
                return url
        if hasattr(entry, "media_thumbnail") and entry.media_thumbnail:
            url = entry.media_thumbnail[0].get("url")
            if url:
                return url
        for link in entry.get("links", []):
            if str(link.get("type", "")).startswith("image/"):
                return link.get("href", "")
        if "enclosures" in entry:
            for enc in entry.enclosures:
                if str(enc.get("type", "")).startswith("image/"):
                    return enc.get("href", "") or enc.get("url", "")
        summary_html = entry.get("summary", "") or entry.get("description", "")
        match = re.search(r'<img[^>]+src="([^\"]+)"', summary_html)
        if match:
            return match.group(1)
    except Exception:
        pass
    return ""


def post_summary_with_telegram_media(client, target_entity, raw_text: str, 
                                     source_label: str, msg, title: str = "") -> bool:
    """Like post_summary, but attaches the original Telegram message's photo/media
    instead of a URL-based image.
    
    Format includes emoji, headline, full context summary, and source.
    """
    summary_en = summarize(raw_text, title)
    headline = extract_headline(title) if title else None
    category_emoji = guess_category(title or "", raw_text)
    
    posted_any = False
    for lang in ["en", "am", "om"]:
        translated_summary = translate(summary_en, lang)
        if translated_summary is None:
            logger.warning("Skipping post (%s) from %s: translation failed", lang, source_label)
            continue

        translated_headline = None
        if headline:
            translated_headline = translate(headline, lang)
            if translated_headline is None:
                translated_headline = headline

        # Build attractive message format
        parts = [LANG_LABELS[lang]]
        
        if translated_headline:
            parts.append(f"{category_emoji} {translated_headline}")
        
        parts.append(f"\n{translated_summary}")
        parts.append(f"\n— {source_label}")
        
        post_text = "\n".join(parts)
        
        try:
            logger.debug("Sending media message to %r", getattr(target_entity, 'username', target_entity))
            client.send_file(target_entity, msg.media, caption=post_text)
            logger.info("Posted (%s) with media from %s", lang, source_label)
            posted_any = True
        except Exception as e:
            logger.warning("Media send failed for %s, falling back to text-only: %s", source_label, e)
            try:
                client.send_message(target_entity, post_text)
                posted_any = True
            except Exception as e2:
                logger.exception("Failed to send message (%s) from %s: %s", lang, source_label, e2)
        sleep_between = int(os.environ.get("SLEEP_BETWEEN_LANGS", "60"))
        if sleep_between > 0:
            time.sleep(sleep_between)

    return posted_any


def process_rss_feeds(client, target_entity, state):
    """Process RSS feeds and post new articles."""
    global_seen = set(state.get("posted_hashes", []))

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

            dedup_key = make_dedup_key(title or raw)
            if dedup_key in global_seen:
                logger.info("Skipping duplicate story from %s: %s", source_name, title[:60])
                new_seen_ids.append(entry_id)
                continue

            image_url = extract_image_url(entry)
            summary_only = clean_text(summary_raw) or raw
            posted = post_summary(client, target_entity, summary_only, source_name, 
                                title=title, image_url=image_url)
            if posted:
                new_seen_ids.append(entry_id)
                global_seen.add(dedup_key)
            # if nothing posted (e.g. send failed), don't mark as seen - retry next run

        state[seen_key] = new_seen_ids[-100:]

    state["posted_hashes"] = list(global_seen)[-500:]


def process_telegram_channels(client, target_entity, state):
    """Process Telegram channels and post new messages."""
    global_seen = set(state.get("posted_hashes", []))

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
                # already processed in a previous run; don't retry indefinitely
                continue

            raw = clean_text(msg.message)
            if not raw:
                newest_id = max(newest_id, msg.id)
                continue

            dedup_key = make_dedup_key(raw)
            if dedup_key in global_seen:
                logger.info("Skipping duplicate story from %s", channel)
                newest_id = max(newest_id, msg.id)
                continue

            has_photo = bool(getattr(msg, "photo", None))
            posted = False
            if has_photo:
                posted = post_summary_with_telegram_media(client, target_entity, raw, channel, msg)
            else:
                posted = post_summary(client, target_entity, raw, channel)
            if posted:
                newest_id = max(newest_id, msg.id)
                global_seen.add(dedup_key)
            # if nothing posted, don't advance newest_id - retry this message next run

        state[f"tg:{channel}"] = newest_id

    state["posted_hashes"] = list(global_seen)[-500:]


def resolve_target_entity(client, target_str):
    """Try several ways to resolve the target channel/entity and return it.

    Accepts:
     - public username (with or without @)
     - numeric id (-100...)
     - t.me link
    """
    if not target_str:
        raise ValueError("Empty TARGET_CHANNEL")

    tried = []
    # Try as-is first
    try:
        ent = client.get_entity(target_str)
        logger.debug("Resolved target_entity as-is: %r", getattr(ent, 'title', ent))
        return ent
    except Exception as e:
        tried.append(f"as-is: {e}")

    # t.me links
    if target_str.startswith("https://t.me/") or target_str.startswith("http://t.me/"):
        short = target_str.rstrip("/\n").rsplit("/", 1)[-1]
        try:
            ent = client.get_entity(short)
            logger.debug("Resolved target_entity from t.me link: %r", getattr(ent, 'title', ent))
            return ent
        except Exception as e:
            tried.append(f"t.me: {e}")

    # @username
    if target_str.startswith("@"):
        try:
            ent = client.get_entity(target_str[1:])
            logger.debug("Resolved target_entity from @username: %r", getattr(ent, 'title', ent))
            return ent
        except Exception as e:
            tried.append(f"@username: {e}")

    # numeric id
    try:
        if target_str.isdigit() or (target_str.startswith("-100") and len(target_str) > 4):
            ent = client.get_entity(int(target_str))
            logger.debug("Resolved target_entity from numeric id: %r", getattr(ent, 'title', ent))
            return ent
    except Exception as e:
        tried.append(f"numeric: {e}")

    logger.error("Failed to resolve TARGET_CHANNEL '%s'. Attempts: %s", target_str, "; ".join(tried))
    raise


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

    # Start the client explicitly for clearer errors
    try:
        client.start()
    except Exception:
        logger.exception("Failed to start Telegram client. Check SESSION_STRING and API credentials")
        raise

    with client:
        try:
            logger.info("Resolving target channel: %s", TARGET_CHANNEL)
            target_entity = resolve_target_entity(client, TARGET_CHANNEL)
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
