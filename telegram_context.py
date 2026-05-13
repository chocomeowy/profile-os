"""
Shared Telegram context helpers for weekly and daily OS scripts.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import requests


def fetch_recent_telegram_notes(
    bot_token: str,
    chat_id: str,
    now_utc: datetime,
    days: int = 7,
    *,
    acknowledge: bool = False,
) -> tuple[list[str], int, int]:
    """
    Fetch pending Telegram messages from this chat in the recent window.

    Telegram getUpdates only returns unacknowledged incoming updates. When
    acknowledge=False, this intentionally avoids advancing the offset so daily
    prompts can read context without consuming notes needed by the weekly brief.
    """
    url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
    cutoff = now_utc - timedelta(days=days)
    messages: list[str] = []
    max_update_id = 0

    params: dict = {"timeout": 0, "limit": 100}
    try:
        resp = requests.get(url, params=params, timeout=15)
        data = resp.json()
    except Exception as e:
        print(f"[Telegram] getUpdates error: {e}")
        return [], 0, 0

    if not data.get("ok") or not data.get("result"):
        return [], 0, 0

    updates = data["result"]
    for update in updates:
        uid = update["update_id"]
        max_update_id = max(max_update_id, uid)

        msg = update.get("message") or update.get("edited_message")
        if not msg:
            continue
        if str(msg.get("chat", {}).get("id", "")) != str(chat_id):
            continue

        msg_dt = datetime.fromtimestamp(msg["date"], tz=timezone.utc)
        if msg_dt < cutoff:
            continue

        text = msg.get("text", "").strip()
        if text:
            ts = msg_dt.strftime("%Y-%m-%d %H:%M")
            messages.append(f"[{ts}] {text}")

    if acknowledge and max_update_id:
        acknowledge_telegram_updates(bot_token, max_update_id)

    return messages, len(updates), max_update_id


def acknowledge_telegram_updates(bot_token: str, max_update_id: int) -> None:
    """Advance the Telegram offset to clear all processed updates."""
    if max_update_id == 0:
        return
    url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
    try:
        requests.get(
            url, params={"offset": max_update_id + 1, "timeout": 0}, timeout=15
        )
        print(f"[Telegram] Acknowledged updates up to {max_update_id}.")
    except Exception as e:
        print(f"[Telegram] Acknowledge failed: {e}")


def summarize_reply_signals(replies: list[str]) -> list[str]:
    """
    Add a deterministic, compact layer of interpretation before the LLM.

    This keeps terse replies like "Yup applied more" from being missed when the
    model evaluates weekly goal status or chooses the next daily nudge.
    """
    signals: list[str] = []
    combined = " ".join(replies).lower()

    if any(word in combined for word in ["applied", "applications", "application"]):
        signals.append("Job applications: user reported applications were submitted this week.")
    if any(word in combined for word in ["hr", "interview", "interviews", "scheduled calls"]):
        signals.append("Job pipeline: user reported HR calls/interviews scheduled for next week.")
    if "finance module" in combined or "unsupervised ml" in combined:
        signals.append("Learning: finance/ML module is active, with unsupervised ML starting next.")
    if any(word in combined for word in ["workout", "bodyweight", "fish", "veggie", "vegetable"]):
        signals.append("Health: user mentioned workout, fish, or veggie target status.")

    return signals
