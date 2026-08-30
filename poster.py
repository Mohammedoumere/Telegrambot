"""
Telegram Tech News Auto-Poster (improved logging & reliability)
--------------------------------
Reads new posts from a list of source Telegram channels, keeps only
technology-related posts, makes a short summary (no links), translates
it into English / Amharic / Afaan Oromoo, and posts each language as
its own separate message (never mixed) to your target channel.

This version adds clearer logging, env checks, resolves the target
entity once, and uses safer sending/forwarding with detailed exception
logging so GitHub Actions logs show the real errors (invalid session,
permission issues, entity not found, etc.).
"""

import os
import re
import json
import time
import logging
from telethon.sync import TelegramClient
from telethon.sessions import StringSession
from deep_translator import GoogleTranslator

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# ---------- Config from GitHub Secrets (env vars) ----------
# Use get() so we can print clearer messages if something is missing
try:
    API_ID = int(os.environ.get("API_ID") or "")
    API_HASH = os.environ.get("API_HASH") or ""
    SESSION_STRING = os.environ.get("SESSION_STRING") or ""
    SOURCE_CHANNELS = [c.strip() for c in (os.environ.get("SOURCE_CHANNELS") or "").split(",") if c.strip()]
    TARGET_CHANNEL = (os.environ.get("TARGET_CHANNEL") or "").strip()
    MAX_PER_CHANNEL = int(os.environ.get("MAX_PER_CHANNEL", "3"))  # newest posts to check each run
except Exception:
    logger.exception("Error reading environment variables")
    raise

STATE_FILE = "state.json"

TECH_KEYWORDS = [
    "tech", "technology", "software", "app", "ai", "artificial intelligence",
    "internet", "digital", "startup", "innovation", "cyber", "data",
    "computer", "smartphone", "5g", "telecom", "fintech", "robot",
    "ethio telecom", "safaricom", "e-commerce", "cloud", "app store",
    "google", "microsoft", "meta", "openai", "chatgpt", "gadget",
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
    # remove links, remove @mentions, collapse whitespace
    text = re.sub(r"http\S+|www\.\S+", "", text)
    text = re.sub(r"@\w+", "", text)
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


def main():
    # Basic env checks to fail fast with a helpful log message
    missing = [v for v in ("API_ID", "API_HASH", "SESSION_STRING", "SOURCE_CHANNELS", "TARGET_CHANNEL") if not locals().get(v)]
    # locals() trick: construct list above then test values
    # fallback manual check (locals() may not contain API_HASH etc. as keys in some contexts)
    if not API_HASH or not SESSION_STRING or not SOURCE_CHANNELS or not TARGET_CHANNEL:
        logger.error("One or more required environment variables are missing or empty.\n  API_ID: %s\n  API_HASH: %s\n  SESSION_STRING: %s\n  SOURCE_CHANNELS: %s\n  TARGET_CHANNEL: %s",
                     bool(API_ID), bool(API_HASH), bool(SESSION_STRING), SOURCE_CHANNELS, TARGET_CHANNEL)
        raise SystemExit(1)

    state = load_state()
    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

    with client:
        try:
            # Resolve the target channel once; this surfaces permission/entity issues early
            logger.info("Resolving target channel: %s", TARGET_CHANNEL)
            target_entity = client.get_entity(TARGET_CHANNEL)
            logger.info("Target resolved: %s", getattr(target_entity, 'title', TARGET_CHANNEL))
        except Exception as e:
            logger.exception("Failed to resolve TARGET_CHANNEL '%s': %s", TARGET_CHANNEL, e)
            raise

        logger.info("Checking %d source channels, max %d messages each", len(SOURCE_CHANNELS), MAX_PER_CHANNEL)

        for channel in SOURCE_CHANNELS:
            last_id = state.get(channel, 0)
            newest_id = last_id
            try:
                messages = list(client.iter_messages(channel, limit=MAX_PER_CHANNEL))
            except Exception as e:
                logger.exception("Failed to fetch messages from %s: %s", channel, e)
                continue

            # oldest first, so channel posting order stays chronological
            messages.reverse()

            for msg in messages:
                try:
                    if not getattr(msg, 'message', None):
                        continue
                    if msg.id <= last_id:
                        continue

                    newest_id = max(newest_id, msg.id)
                    raw = clean_text(msg.message)

                    if not raw or not is_tech(raw):
                        continue

                    summary_en = summarize(raw)

                    # Check if message has a photo/media
                    has_media = getattr(msg, 'photo', None) or getattr(msg, 'media', None)

                    for lang in ["en", "am", "om"]:
                        translated = translate(summary_en, lang)
                        post_text = f"{LANG_LABELS[lang]}\n\n{translated}"

                        try:
                            if has_media:
                                # Prefer forwarding the original message (preserves media & attribution)
                                try:
                                    client.forward_messages(target_entity, msg)
                                    logger.info("Forwarded media message %s to %s (%s)", msg.id, TARGET_CHANNEL, lang)
                                except Exception as e:
                                    logger.warning("Forward failed for msg %s: %s - trying send_file fallback", msg.id, e)
                                    # Fallback: try sending media with caption
                                    try:
                                        client.send_file(target_entity, msg.media, caption=post_text)
                                        logger.info("Sent media (fallback) for msg %s to %s (%s)", msg.id, TARGET_CHANNEL, lang)
                                    except Exception as e2:
                                        logger.exception("Failed to send media fallback for msg %s: %s", msg.id, e2)
                                        # Final fallback: send text only
                                        client.send_message(target_entity, post_text)
                                        logger.info("Sent text-only fallback for msg %s to %s (%s)", msg.id, TARGET_CHANNEL, lang)
                            else:
                                client.send_message(target_entity, post_text)
                                logger.info("Sent text message for msg %s to %s (%s)", msg.id, TARGET_CHANNEL, lang)

                        except Exception as e:
                            logger.exception("Failed to post message %s to %s: %s", msg.id, TARGET_CHANNEL, e)

                        time.sleep(3)  # small gap between the 3 language posts

                except Exception:
                    logger.exception("Unexpected error while processing a message from %s", channel)

            state[channel] = newest_id

    save_state(state)


if __name__ == "__main__":
    main()
