"""
Daily Prompt - Personal Operating System Daily Nudge
====================================================
Reads profile.md from Google Drive, uses Gemini to generate a relevant 
daily question or nudge, and sends it to Telegram with quick-reply buttons.
"""

import json
import os
import sys
import requests
from datetime import datetime, timezone
import google.generativeai as genai
from telegram_context import fetch_recent_telegram_notes, summarize_reply_signals
from drive_context import (
    build_drive_service,
    read_drive_file,
    get_or_create_inbox_file_id,
    load_inbox_messages,
    save_inbox_messages,
)
from llm_context import generate_with_fallback

# ── Config ────────────────────────────────────────────────────────────────────

def _clean_secret(v: str) -> str:
    return v.strip().replace("\xa0", " ") if v else v

GDRIVE_FILE_ID           = _clean_secret(os.environ.get("GDRIVE_FILE_ID", ""))
GDRIVE_SA_JSON           = os.environ.get("GDRIVE_SERVICE_ACCOUNT_JSON", "")
GEMINI_API_KEY           = _clean_secret(os.environ.get("GEMINI_API_KEY", ""))
TELEGRAM_BOT_TOKEN       = _clean_secret(os.environ.get("TELEGRAM_BOT_TOKEN", ""))
TELEGRAM_CHAT_ID         = _clean_secret(str(os.environ.get("TELEGRAM_CHAT_ID", "")))

NOW_UTC    = datetime.now(timezone.utc)
TODAY      = NOW_UTC.strftime("%A, %d %B %Y")

REQUIRED_VARS = [
    "GDRIVE_FILE_ID",
    "GDRIVE_SERVICE_ACCOUNT_JSON",
    "GEMINI_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
]

def validate_env() -> None:
    missing = [v for v in REQUIRED_VARS if not os.environ.get(v)]
    if missing:
        print("[Daily Prompt] ERROR: Missing or empty required environment variables:")
        for v in missing:
            print(f"  - {v}")
        sys.exit(1)

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]

# ── Google Drive ──────────────────────────────────────────────────────────────

def fetch_profile(service) -> str:
    return read_drive_file(service, GDRIVE_FILE_ID)

# ── Telegram ──────────────────────────────────────────────────────────────────

def send_telegram(message: str, options: list[str] = None) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
    }
    if options:
        # Arrange options: 2 buttons per row for nice spacing
        keyboard = []
        row = []
        for opt in options:
            row.append({"text": opt})
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
            
        payload["reply_markup"] = {
            "keyboard": keyboard,
            "resize_keyboard": True,
            "one_time_keyboard": True,
        }
    else:
        # Clear custom keyboard
        payload["reply_markup"] = {"remove_keyboard": True}

    resp = requests.post(url, json=payload, timeout=15)
    if not resp.ok:
        print(f"[Telegram] Send failed: {resp.status_code} {resp.text}")
        return False
    return True

# ── Gemini ────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are a helpful, direct personal OS assistant.
Your goal is to send a short, engaging daily nudge or question to the user via Telegram
to help them stay on track with their goals and habits, along with 2-4 quick-reply options.

You MUST respond with a JSON object containing exactly two keys: "nudge" and "options".

Example JSON response:
{
  "nudge": "Did you work on your unsupervised ML module today?",
  "options": ["Yes, completed! 🧠", "Not yet, tonight 🌙", "Skip today ⏭️"]
}

Rules for "nudge":
1. Read the user's profile.
2. Read recent Telegram replies as ground-truth status updates.
3. Pick ONE specific goal or habit that still needs attention today.
4. Do not repeat a question that recent replies already answered.
5. If job applications were already reported, shift to next useful step such as interview prep, follow-up, or learning priority.
6. If the user reported HR calls/interviews scheduled, acknowledge the pipeline and ask about preparation, not whether applications were sent.
7. Keep it under 200 characters.
8. Be direct but friendly. No "AI assistant" filler.
9. Use a casual, supportive tone.
10. If it's a weekend, you can be slightly more reflective. If it's a weekday, be more action-oriented.

Rules for "options":
1. Provide 2 to 4 quick-reply button options that correspond naturally to the nudge question.
2. The options should let the user answer with a single tap.
3. Keep each option very short (1-3 words + a relevant emoji).
4. Always include a low-pressure escape option at the end (e.g., "Skip today ⏭️" or "No updates 🤫").
"""

def generate_prompt(profile_content: str, recent_replies: list[str], api_key: str) -> str:
    parts = [f"Today is {TODAY}.", "", "## User Profile", profile_content]
    if recent_replies:
        reply_signals = summarize_reply_signals(recent_replies)
        if reply_signals:
            parts += ["", "## Interpreted Recent Telegram Signals", *[f"- {s}" for s in reply_signals]]
        parts += [
            "",
            "## Recent Telegram Replies",
            "Use these replies to avoid stale or already-answered nudges.",
            *recent_replies,
        ]
    else:
        parts += ["", "## Recent Telegram Replies", "No recent replies are pending."]

    prompt_text = "\n".join(parts)
    
    try:
        # Request JSON output
        gen_config = {"response_mime_type": "application/json"}
        return generate_with_fallback(
            api_key,
            prompt_text,
            SYSTEM_PROMPT,
            generation_config=gen_config,
        ).strip()
    except Exception as e:
        raise RuntimeError(f"All Gemini models failed. Last error: {e}")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    validate_env()
    print(f"[Daily Prompt] Generating nudge for {TODAY}")
    
    drive = build_drive_service(GDRIVE_SA_JSON, DRIVE_SCOPES)
    profile = fetch_profile(drive)
    
    # Get or create the Google Drive inbox file ID inside parent of profile.md
    inbox_file_id = get_or_create_inbox_file_id(
        drive,
        profile_file_id=GDRIVE_FILE_ID,
        owner_email=os.environ.get("OWNER_EMAIL") or os.environ.get("EMAIL_ADDRESS")
    )
    
    # Load existing replies from Google Drive
    drive_replies = load_inbox_messages(drive, inbox_file_id)
    print(f"      Existing replies in Google Drive inbox: {len(drive_replies)}")
    
    # Fetch new replies from Telegram and acknowledge them immediately to prevent 24h expiration
    new_replies, total_updates, _ = fetch_recent_telegram_notes(
        TELEGRAM_BOT_TOKEN,
        TELEGRAM_CHAT_ID,
        NOW_UTC,
        days=7,
        acknowledge=True,
    )
    print(f"      New Telegram replies found: {len(new_replies)}")
    if total_updates > len(new_replies):
        print(f"      (Note: {total_updates - len(new_replies)} update(s) were skipped/filtered).")
    
    # Merge and update Google Drive inbox
    all_replies = drive_replies
    if new_replies:
        all_replies.extend(new_replies)
        # De-duplicate while preserving order
        seen = set()
        all_replies = [r for r in all_replies if not (r in seen or seen.add(r))]
        save_inbox_messages(drive, inbox_file_id, all_replies)
        print(f"      Saved merged replies list to Google Drive inbox ({len(all_replies)} total).")
    
    # Generate the daily prompt using accumulated context
    nudge_raw = generate_prompt(profile, all_replies, GEMINI_API_KEY)
    
    # Robustly parse structured JSON output
    nudge_text = ""
    options = []
    try:
        data = json.loads(nudge_raw)
        nudge_text = data.get("nudge", "").strip()
        options = data.get("options", [])
    except Exception as e:
        print(f"      [Daily Prompt] Failed to parse JSON response. Raw content: {nudge_raw}. Error: {e}")
        # Graceful fallback to plain text and default quick replies
        nudge_text = nudge_raw.strip()
        options = ["Done! ✅", "Not yet ⏳", "Skip today ⏭️", "Stuck / no progress ❌"]
        
    if not nudge_text:
        nudge_text = "Checking in: how are your habits and goals coming along today?"
        options = ["Great! ✅", "Progressing 📈", "Skip today ⏭️"]

    print(f"      Nudge text: {nudge_text}")
    print(f"      Button options: {options}")
    
    if send_telegram(nudge_text, options):
        print("      Telegram: OK")
    else:
        print("      Telegram: FAILED")
        sys.exit(1)

if __name__ == "__main__":
    main()
