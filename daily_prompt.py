"""
Daily Prompt - Personal Operating System Daily Nudge
====================================================
Reads profile.md from Google Drive, uses Gemini to generate a relevant 
daily question or nudge, and sends it to Telegram.
"""

import json
import os
import sys
import requests
from datetime import datetime, timezone
import google.generativeai as genai
from google.oauth2 import service_account
from googleapiclient.discovery import build

from telegram_context import fetch_recent_telegram_notes, summarize_reply_signals

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

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

# ── Google Drive ──────────────────────────────────────────────────────────────

def build_drive_service():
    sa_info = json.loads(GDRIVE_SA_JSON)
    creds = service_account.Credentials.from_service_account_info(
        sa_info, scopes=DRIVE_SCOPES
    )
    return build("drive", "v3", credentials=creds)

def fetch_profile(service) -> str:
    file_metadata = service.files().get(fileId=GDRIVE_FILE_ID, fields="mimeType").execute()
    mime_type = file_metadata.get("mimeType", "")
    if mime_type.startswith("application/vnd.google-apps."):
        request = service.files().export_media(fileId=GDRIVE_FILE_ID, mimeType="text/plain")
    else:
        request = service.files().get_media(fileId=GDRIVE_FILE_ID)
    content = request.execute()
    return content.decode("utf-8") if isinstance(content, bytes) else content

# ── Telegram ──────────────────────────────────────────────────────────────────

def send_telegram(message: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(
        url,
        json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"},
        timeout=15,
    )
    if not resp.ok:
        print(f"[Telegram] Send failed: {resp.status_code} {resp.text}")
        return False
    return True

# ── Gemini ────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are a helpful, direct personal OS assistant. 
Your goal is to send a short, engaging daily nudge or question to the user via Telegram 
to help them stay on track with their goals and habits.

Rules:
1. Read the user's profile.
2. Read recent Telegram replies as ground-truth status updates.
3. Pick ONE specific goal or habit that still needs attention today.
4. Do not repeat a question that recent replies already answered.
5. If job applications were already reported, shift to next useful step such as interview prep, follow-up, or learning priority.
6. If the user reported HR calls/interviews scheduled, acknowledge the pipeline and ask about preparation, not whether applications were sent.
7. Generate a single question or encouraging nudge (max 200 characters).
8. Be direct but friendly. No "AI assistant" filler. 
9. Use a casual, supportive tone.
10. If it's a weekend, you can be slightly more reflective. If it's a weekday, be more action-oriented.
"""

def generate_prompt(profile_content: str, recent_replies: list[str], api_key: str) -> str:
    genai.configure(api_key=api_key)
    models_to_try = [
        "gemini-3.1-flash-lite-preview",
        "gemini-2.0-flash-lite",
        "gemini-1.5-flash-8b",
        "gemini-1.5-flash",
        "gemini-2.0-flash-exp",
    ]
    
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
    
    last_error = None
    for model_name in models_to_try:
        try:
            print(f"      Attempting generation with {model_name}...")
            model = genai.GenerativeModel(
                model_name=model_name,
                system_instruction=SYSTEM_PROMPT,
            )
            response = model.generate_content(prompt_text)
            return response.text.strip()
        except Exception as e:
            print(f"      {model_name} failed: {e}")
            last_error = e
            continue
            
    raise RuntimeError(f"All Gemini models failed. Last error: {last_error}")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    validate_env()
    print(f"[Daily Prompt] Generating nudge for {TODAY}")
    
    drive = build_drive_service()
    profile = fetch_profile(drive)
    replies, total_updates, _ = fetch_recent_telegram_notes(
        TELEGRAM_BOT_TOKEN,
        TELEGRAM_CHAT_ID,
        NOW_UTC,
        days=7,
        acknowledge=False,
    )
    print(f"      Recent Telegram replies found: {len(replies)}")
    if total_updates > len(replies):
        print(f"      (Note: {total_updates - len(replies)} update(s) were skipped/filtered).")
    
    nudge = generate_prompt(profile, replies, GEMINI_API_KEY)
    print(f"      Nudge generated: {nudge}")
    
    if send_telegram(nudge):
        print("      Telegram: OK")
    else:
        print("      Telegram: FAILED")
        sys.exit(1)

if __name__ == "__main__":
    main()
