"""
Telegram Tech News Auto-Poster
--------------------------------
Reads new posts from a list of source Telegram channels, keeps only
technology-related posts, makes a short summary (no links), translates
it into English / Amharic / Afaan Oromoo, and posts each language as
its own separate message (never mixed) to your target channel.

Runs from GitHub Actions on a schedule, so it works even if your
phone is offline.
"""

import os
import re
import json
import time
from telethon.sync import TelegramClient
from telethon.sessions import StringSession
from deep_translator import GoogleTranslator

# ---------- Config from GitHub Secrets (env vars) ----------
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION_STRING = os.environ["SESSION_STRING"]
SOURCE_CHANNELS = [c.strip() for c in os.environ["SOURCE_CHANNELS"].split(",") if c.strip()]
TARGET_CHANNEL = os.environ["TARGET_CHANNEL"].strip()
MAX_PER_CHANNEL = int(os.environ.get("MAX_PER_CHANNEL", "3"))  # newest posts to check each run

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
        print(f"Translation to {target} failed: {e}")
        return text


def main():
    state = load_state()
    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

    with client:
        for channel in SOURCE_CHANNELS:
            last_id = state.get(channel, 0)
            newest_id = last_id
            messages = list(client.iter_messages(channel, limit=MAX_PER_CHANNEL))
            # oldest first, so channel posting order stays chronological
            messages.reverse()

            for msg in messages:
                if not msg.message:
                    continue
                if msg.id <= last_id:
                    continue

                newest_id = max(newest_id, msg.id)
                raw = clean_text(msg.message)

                if not raw or not is_tech(raw):
                    continue

                summary_en = summarize(raw)

                for lang in ["en", "am", "om"]:
                    translated = translate(summary_en, lang)
                    post_text = f"{LANG_LABELS[lang]}\n\n{translated}"
                    client.send_message(TARGET_CHANNEL, post_text)
                    time.sleep(3)  # small gap between the 3 language posts

            state[channel] = newest_id

    save_state(state)


if __name__ == "__main__":
    main()
