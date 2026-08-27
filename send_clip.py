"""
Send a random (never-repeating until exhausted) Twitch clip
to Telegram and Discord, twice a week, at 17:00 Kyiv time.

Meant to run via GitHub Actions on an hourly cron; the script itself
decides whether "now" is the right moment (handles Kyiv DST automatically).

Required environment variables (set as GitHub Secrets):
  TWITCH_CLIENT_ID
  TWITCH_CLIENT_SECRET
  TWITCH_BROADCASTER_ID       -> numeric Twitch user id of your channel
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID            -> where the clip gets posted (channel/group)
  TELEGRAM_PERSONAL_CHAT_ID   -> your personal chat id, for error alerts
  DISCORD_WEBHOOK_URL

Optional:
  FORCE_RUN=1                 -> skip the day/time check (useful for manual testing)
"""

import json
import os
import random
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

STATE_FILE = Path("sent_clips.json")


def env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value
  

def get_twitch_app_token(client_id: str, client_secret: str) -> str:
    resp = requests.post(
        "https://id.twitch.tv/oauth2/token",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def get_all_clips(client_id: str, token: str, broadcaster_id: str) -> list[dict]:
    clips = []
    cursor = None
    headers = {"Client-Id": client_id, "Authorization": f"Bearer {token}"}

    while True:
        params = {"broadcaster_id": broadcaster_id, "first": 100}
        if cursor:
            params["after"] = cursor

        resp = requests.get(
            "https://api.twitch.tv/helix/clips",
            headers=headers,
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        payload = resp.json()
        clips.extend(payload.get("data", []))

        cursor = payload.get("pagination", {}).get("cursor")
        if not cursor:
            break

    return clips


def load_sent_ids() -> set[str]:
    if not STATE_FILE.exists():
        return set()
    try:
        return set(json.loads(STATE_FILE.read_text()))
    except (json.JSONDecodeError, ValueError):
        return set()


def save_sent_ids(ids: set[str]) -> None:
    STATE_FILE.write_text(json.dumps(sorted(ids), indent=2, ensure_ascii=False))


def pick_random_clip(all_clips: list[dict]) -> tuple[dict, set[str]]:
    sent_ids = load_sent_ids()
    remaining = [c for c in all_clips if c["id"] not in sent_ids]

    if not remaining:
        # Full reset: forget history completely, pool becomes fully random again
        sent_ids = set()
        remaining = all_clips

    if not remaining:
        raise RuntimeError("No clips found on the channel at all.")

    chosen = random.choice(remaining)
    sent_ids.add(chosen["id"])
    return chosen, sent_ids


def send_telegram(bot_token: str, chat_id: str, text: str) -> None:
    resp = requests.post(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "disable_web_page_preview": False},
        timeout=15,
    )
    resp.raise_for_status()


def send_discord(webhook_url: str, content: str) -> None:
    resp = requests.post(webhook_url, json={"content": content}, timeout=15)
    resp.raise_for_status()


def notify_failure(error: Exception) -> None:
    """Best-effort personal alert. Never raises further."""
    try:
        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
        personal_chat_id = os.environ.get("TELEGRAM_PERSONAL_CHAT_ID")
        if bot_token and personal_chat_id:
            send_telegram(
                bot_token,
                personal_chat_id,
                f"⚠️ Clip bot failed: {type(error).__name__}: {error}",
            )
    except Exception:
        pass  # swallow — we're already in an error path


def main() -> None:

    client_id = env("TWITCH_CLIENT_ID")
    client_secret = env("TWITCH_CLIENT_SECRET")
    broadcaster_id = env("TWITCH_BROADCASTER_ID")
    bot_token = env("TELEGRAM_BOT_TOKEN")
    chat_id = env("TELEGRAM_CHAT_ID")
    discord_webhook = env("DISCORD_WEBHOOK_URL")

    token = get_twitch_app_token(client_id, client_secret)
    all_clips = get_all_clips(client_id, token, broadcaster_id)

    clip, updated_sent_ids = pick_random_clip(all_clips)
    url = clip["url"]
    title = clip.get("title", "Random clip")
    message = f"🎬 {title}\n{url}"

    send_telegram(bot_token, chat_id, message)
    send_discord(discord_webhook, message)

    # Only persist state after a successful send
    save_sent_ids(updated_sent_ids)
    print(f"Sent clip {clip['id']} successfully.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        notify_failure(e)
        sys.exit(1)
