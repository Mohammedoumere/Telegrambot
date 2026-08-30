"""
Telegram Tech News Auto-Poster with Web Scraping (improved logging & reliability)
--------------------------------
Reads new posts from a list of source Telegram channels, keeps only
technology-related posts, makes a short summary (no links), translates
it into English / Amharic / Afaan Oromoo, and posts each language as
its own separate message (never mixed) to your target channel.

Also fetches related tech news from Google News and other websites,
extracts source links, and includes them in the posted message.

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
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, quote
import feedparser

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
    # Default RSS sources: Ars Technica, TechCrunch, The Verge.
# You can override/extend this via the RSS_FEEDS secret (comma-separated URLs).
DEFAULT_RSS_FEEDS = [
    "https://feeds.arstechnica.com/arstechnica/index",
    "https://techcrunch.com/feed/",
    "https://www.theverge.com/rss/index.xml",
]
RSS_FEEDS_ENV = os.environ.get("RSS_FEEDS", "").strip()
RSS_FEEDS = [f.strip() for f in RSS_FEEDS_ENV.split(",") if f.strip()] or DEFAULT_RSS_FEEDS
MAX_PER_FEED = int(os.environ.get("MAX_PER_FEED", "3"))


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

# User-Agent to avoid blocks
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
HEADERS = {"User-Agent": USER_AGENT}

# News sources
NEWS_SOURCES = {
    "google_news": "https://news.google.com/news/rss/headlines/section/topic/TECHNOLOGY",
    "techcrunch": "https://feeds.techcrunch.com/",
    "hackernews": "https://news.ycombinator.com/",
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


def fetch_news_from_rss(feed_url: str, max_articles: int = 3) -> list:
    """Fetch news from RSS feed and extract source links."""
    articles = []
    try:
        feed = feedparser.parse(feed_url)
        for entry in feed.entries[:max_articles]:
            article = {
                "title": entry.get("title", ""),
                "link": entry.get("link", ""),
                "summary": entry.get("summary", ""),
                "source": feed.feed.get("title", "RSS Feed")
            }
            if article["title"] and article["link"]:
                articles.append(article)
        logger.info("Fetched %d articles from RSS: %s", len(articles), feed_url)
    except Exception as e:
        logger.warning("Failed to fetch RSS from %s: %s", feed_url, e)
    return articles


def fetch_google_news(keyword: str, max_articles: int = 3) -> list:
    """Fetch news from Google News using RSS."""
    try:
        search_url = f"https://news.google.com/rss/search?q={quote(keyword)}"
        return fetch_news_from_rss(search_url, max_articles)
    except Exception as e:
        logger.warning("Failed to fetch Google News for keyword '%s': %s", keyword, e)
        return []


def fetch_hackernews(max_articles: int = 3) -> list:
    """Fetch top posts from Hacker News with direct links."""
    articles = []
    try:
        response = requests.get(NEWS_SOURCES["hackernews"], headers=HEADERS, timeout=5)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")
        
        # Get top stories
        rows = soup.find_all("tr", class_="athing")[:max_articles]
        
        for row in rows:
            title_cell = row.find("span", class_="titleline")
            if not title_cell:
                continue
                
            link_elem = title_cell.find("a")
            if not link_elem:
                continue
            
            title = link_elem.text.strip()
            link = link_elem.get("href", "")
            
            # Make absolute URL if needed
            if link and not link.startswith("http"):
                link = urljoin(NEWS_SOURCES["hackernews"], link)
            
            if title and link:
                articles.append({
                    "title": title,
                    "link": link,
                    "summary": "",
                    "source": "Hacker News"
                })
        
        logger.info("Fetched %d articles from Hacker News", len(articles))
    except Exception as e:
        logger.warning("Failed to fetch Hacker News: %s", e)
    
    return articles


def fetch_web_news(keywords: list, max_articles: int = 5) -> list:
    """Fetch news from multiple web sources based on keywords."""
    all_articles = []
    
    # Try multiple sources
    for keyword in keywords[:2]:  # Limit to 2 keywords to avoid too many requests
        # Google News
        google_articles = fetch_google_news(keyword, max_articles=2)
        all_articles.extend(google_articles)
        time.sleep(1)  # Be respectful to servers
        
        # TechCrunch RSS
        try:
            techcrunch_articles = fetch_news_from_rss(NEWS_SOURCES["techcrunch"], max_articles=2)
            all_articles.extend(techcrunch_articles)
        except Exception as e:
            logger.warning("TechCrunch fetch failed: %s", e)
        time.sleep(1)
    
    # Hacker News (separate because it requires parsing)
    hn_articles = fetch_hackernews(max_articles=2)
    all_articles.extend(hn_articles)
    
    # Remove duplicates and limit
    seen_titles = set()
    unique_articles = []
    for article in all_articles:
        if article["title"] not in seen_titles:
            seen_titles.add(article["title"])
            unique_articles.append(article)
    
    return unique_articles[:max_articles]


def format_post_with_sources(summary_text: str, source_articles: list = None) -> str:
    """Format post text with source links if available."""
    post = summary_text
    
    if source_articles:
        post += "\n\n📚 **Related Sources:**\n"
        for i, article in enumerate(source_articles[:3], 1):  # Limit to 3 sources
            source_name = article.get("source", "Source")
            link = article.get("link", "")
            if link:
                post += f"{i}. [{source_name}]({link})\n"
    
    return post


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
                    
                    # Extract keywords for web news search
                    keywords = [k for k in TECH_KEYWORDS if k in raw.lower()]
                    if not keywords:
                        keywords = ["technology", "tech news"]
                    
                    # Fetch related web news
                    logger.info("Fetching related web news for keywords: %s", keywords)
                    web_news = fetch_web_news(keywords, max_articles=3)

                    # Check if message has a photo/media
                    has_media = getattr(msg, 'photo', None) or getattr(msg, 'media', None)

                    for lang in ["en", "am", "om"]:
                        translated = translate(summary_en, lang)
                        
                        # Format with source links (translate the label but keep links)
                        if lang == "en":
                            post_text = format_post_with_sources(
                                f"{LANG_LABELS[lang]}\n\n{translated}",
                                web_news
                            )
                        else:
                            # For non-English, translate only the summary, keep source links in English
                            post_text = format_post_with_sources(
                                f"{LANG_LABELS[lang]}\n\n{translated}",
                                web_news
                            )

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
