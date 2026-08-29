# Telegram Tech News Auto-Poster (runs even when your phone is off)

This bot reads new posts from Telegram tech channels you choose, keeps only
technology-related ones, writes a short summary (no links), and posts it to
your channel **three separate times** — once in English, once in Amharic,
once in Afaan Oromoo (never mixed in one message). It runs on GitHub's
servers on a schedule, so it keeps posting even if your phone is offline.

Everything below can be done from your phone.

---

## PART 1 — Get your API ID and API Hash

1. On your phone, open a browser and go to **my.telegram.org**
2. Log in with your phone number (you'll get a login code in Telegram).
3. Tap **API development tools**.
4. Fill in:
   - App title: `TechPoster`
   - Short name: `techposter`
   - Platform: choose "Other"
5. Tap **Create application**.
6. You'll now see:
   - **api_id** (a number)
   - **api_hash** (a long code)
   Copy both somewhere safe (Notes app). You'll paste them into GitHub later.

---

## PART 2 — Generate your Session String (mobile-friendly, no app install)

The session string lets the script log in as you, without scanning a QR
code every time.

1. On your phone browser, go to **colab.research.google.com**
2. Tap **New notebook** (sign in with Google if asked).
3. Tap the `+ Code` cell and paste this:

```python
!pip install telethon -q
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

api_id = 123456          # replace with your api_id
api_hash = "your_api_hash_here"

with TelegramClient(StringSession(), api_id, api_hash) as client:
    print(client.session.save())
```

4. Replace `123456` and `"your_api_hash_here"` with your real values.
5. Tap the ▶️ **Run** button.
6. It will ask for your phone number (with country code, e.g. `+2519...`),
   then a login code sent to your Telegram app, and possibly your 2FA
   password if you have one set.
7. When it finishes, a long string of letters/numbers is printed — that is
   your **SESSION_STRING**. Copy it and save it somewhere safe (treat it
   like a password — anyone with it can log into your Telegram).
8. Close the Colab tab when done (no need to save the notebook).

---

## PART 3 — Create the GitHub repository

1. Install the **GitHub** app from the Play Store / App Store, or just use
   github.com in your browser.
2. Sign up / log in.
3. Tap **+** → **New repository**.
4. Name it e.g. `tech-news-poster`, set it to **Private**, tap **Create**.

## PART 4 — Add the project files

In your new repo, tap **Add file → Create new file** for each of these,
and paste in the matching content (I've attached all files below the chat
for you to copy from):

- `poster.py`
- `requirements.txt`
- `state.json` (just contains `{}`)
- `.github/workflows/post.yml` (type the folder path exactly like this in
  the filename box — GitHub will create the folders automatically)

Commit each file after pasting it in.

## PART 5 — Add your secrets (this keeps your keys private)

In your repo: **Settings → Secrets and variables → Actions → New repository secret**.
Add each of these one at a time:

| Secret name | Value |
|---|---|
| `API_ID` | the api_id number from Part 1 |
| `API_HASH` | the api_hash from Part 1 |
| `SESSION_STRING` | the long string from Part 2 |
| `SOURCE_CHANNELS` | comma-separated usernames of tech channels to read from, e.g. `channel1,channel2,channel3` (no @ symbol) |
| `TARGET_CHANNEL` | your channel's username, e.g. `mychannel` (no @ symbol), or its numeric ID if it's private |

> Your Telegram account must already be an admin (or member, for public
> channels you're just reading) of the source channels, and admin of your
> target channel with permission to post.

## PART 6 — Turn it on

1. Go to the **Actions** tab of your repo.
2. If asked, tap **"I understand my workflows, enable them"**.
3. Tap **Auto Post Tech News** → **Run workflow** to test it manually once.
4. Check your target channel — you should see new tech posts appear, each
   language as its own message.

From now on it runs automatically every 3 hours (edit the `cron` line in
`post.yml` to change frequency), even while your phone is switched off,
because it's running on GitHub's computers, not yours.

---

## Notes / things you can adjust

- **Posting frequency**: change `cron: "0 */3 * * *"` in `post.yml`
  (every 3 hours). E.g. `"0 * * * *"` = every hour.
- **How many recent posts checked per channel per run**: `MAX_PER_CHANNEL`
  (default 3) — add it as an extra secret if you want to change it.
- **Tech keyword filter**: edit the `TECH_KEYWORDS` list inside `poster.py`
  to fine-tune what counts as "technology" news.
- **Translation engine**: uses free Google Translate under the hood via
  `deep-translator`. Quality is good for short summaries but not perfect —
  you can swap in a paid translation API later if you want higher accuracy.
- **No links posted**: the script strips URLs automatically, only the
  summary text is posted, as requested.
- **Keep SESSION_STRING private**: it's equivalent to your Telegram login.
  If you ever think it's leaked, revoke it via Telegram Settings → Devices.
