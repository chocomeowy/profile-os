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
from telegram_context import fetch_recent_telegram_notes, summarize_reply_signals, acknowledge_telegram_updates
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
GDRIVE_GOALS_LOG_FILE_ID = _clean_secret(os.environ.get("GDRIVE_GOALS_LOG_FILE_ID", ""))
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

def parse_message_text(reply_str: str) -> str:
    # Each reply_str is formatted as: "[YYYY-MM-DD HH:MM] message_text"
    if "]" in reply_str:
        return reply_str.split("]", 1)[1].strip()
    return reply_str.strip()


def send_help_message() -> None:
    help_text = (
        "*Profile OS Bot Commands:*\n\n"
        "• `/summary` or `/status` - Generate and send your current weekly brief (read-only).\n"
        "• `/nudge` - Generate and send a new daily nudge question immediately.\n"
        "• `/help` - Show this list of commands.\n\n"
        "Any other message you send will be saved to your Google Drive inbox as a weekly note."
    )
    send_telegram(help_text)


def generate_and_send_nudge(profile: str, all_replies: list[str]) -> bool:
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
        return True
    else:
        print("      Telegram: FAILED")
        return False


def generate_and_send_summary(drive, profile: str, all_replies: list[str]) -> bool:
    print("      Loading tools for summary brief generation...")
    try:
        from weekly_checkin import generate_brief, fetch_goals_log, check_profile_staleness, send_telegram as send_tg_long
    except ImportError as e:
        print(f"      ERROR: Failed to import weekly_checkin. Is weekly_checkin.py present? Error: {e}")
        send_telegram("Could not generate summary: failed to import `weekly_checkin.py` components.")
        return False

    staleness_warning = check_profile_staleness(profile)
    goals_log = fetch_goals_log(drive)
    
    print("      Generating brief with Gemini...")
    try:
        brief = generate_brief(profile, all_replies, goals_log, staleness_warning, GEMINI_API_KEY)
    except Exception as e:
        print(f"      ERROR during summary generation: {e}")
        send_telegram(f"Error generating summary: {e}")
        return False
    
    print("      Sending summary brief to Telegram...")
    if send_tg_long(brief):
        print("      Summary brief sent.")
        return True
    else:
        print("      Failed to send summary brief.")
        return False


def main():
    validate_env()
    
    # Check if we are running in polling mode
    is_polling = "--poll" in sys.argv or os.environ.get("POLL_ONLY") == "true"
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    is_github_schedule = (event_name == "schedule")
    
    # Daily scheduled time is 9:00 AM SGT (01:00 UTC)
    # Check if current time is within daily scheduled window (01:00 - 01:30 UTC)
    is_daily_scheduled_window = (NOW_UTC.hour == 1 and NOW_UTC.minute < 30)
    
    # We should run a daily nudge even if no updates if:
    # 1. It is the scheduled daily window (01:00 UTC)
    # 2. It is not triggered by a GitHub schedule (e.g., triggered manually via UI or local CLI run)
    proceed_anyway = (not is_github_schedule) or is_daily_scheduled_window or (not is_polling)
    
    print(f"[Daily Prompt] Starting run. is_polling={is_polling}, is_github_schedule={is_github_schedule}, proceed_anyway={proceed_anyway}")
    
    # Fetch new replies from Telegram without acknowledging them yet
    new_replies, total_updates, max_update_id = fetch_recent_telegram_notes(
        TELEGRAM_BOT_TOKEN,
        TELEGRAM_CHAT_ID,
        NOW_UTC,
        days=7,
        acknowledge=False,
    )
    
    # Acknowledge updates immediately to clear them from Telegram's queue
    if max_update_id > 0:
        acknowledge_telegram_updates(TELEGRAM_BOT_TOKEN, max_update_id)
        
    has_new_content = len(new_replies) > 0
    print(f"      Total updates: {total_updates}, Max update ID: {max_update_id}")
    print(f"      New user messages found: {len(new_replies)}")
    
    if not has_new_content:
        if not proceed_anyway:
            print(f"      No updates found at {NOW_UTC.strftime('%H:%M')} UTC. Exiting.")
            sys.exit(0)
            
    # Connect to Drive
    drive = build_drive_service(GDRIVE_SA_JSON, DRIVE_SCOPES)
    profile = fetch_profile(drive)
    
    # Get or create the Google Drive inbox file ID inside parent of profile.md
    inbox_file_id = get_or_create_inbox_file_id(
        drive,
        profile_file_id=GDRIVE_FILE_ID,
        owner_email=os.environ.get("OWNER_EMAIL") or os.environ.get("EMAIL_ADDRESS")
    )
    
    # Split new replies into commands and normal replies
    normal_replies = []
    commands = []
    for reply in new_replies:
        text = parse_message_text(reply)
        if text.startswith("/"):
            commands.append((reply, text))
        else:
            normal_replies.append(reply)
            
    # Load existing replies from Google Drive
    drive_replies = load_inbox_messages(drive, inbox_file_id)
    print(f"      Existing replies in Google Drive inbox: {len(drive_replies)}")
    
    # Merge and update Google Drive inbox if we have new normal replies
    all_replies = drive_replies
    if normal_replies:
        all_replies = drive_replies + normal_replies
        seen = set()
        all_replies = [r for r in all_replies if not (r in seen or seen.add(r))]
        save_inbox_messages(drive, inbox_file_id, all_replies)
        print(f"      Saved merged replies list to Google Drive inbox ({len(all_replies)} total).")
        
    # Route execution based on commands or polling mode
    if commands:
        # Execute the latest command
        latest_reply, latest_text = commands[-1]
        cmd_parts = latest_text.split()
        cmd_name = cmd_parts[0].lower()
        
        if cmd_name in ["/summary", "/status"]:
            generate_and_send_summary(drive, profile, all_replies)
        elif cmd_name == "/nudge":
            generate_and_send_nudge(profile, all_replies)
        elif cmd_name == "/help":
            send_help_message()
        else:
            send_telegram(f"Unknown command: `{cmd_name}`. Send `/help` for available commands.")
            
    elif has_new_content:
        # We had new updates but they were all normal replies
        if not proceed_anyway:
            # Confirm receipt on Telegram
            if len(normal_replies) == 1:
                clean_text = parse_message_text(normal_replies[0])
                send_telegram(f"Logged note: \"{clean_text}\" 📝")
            else:
                send_telegram(f"Logged {len(normal_replies)} notes to Drive inbox. 📝")
        else:
            # Scheduled or manual daily prompt run
            generate_and_send_nudge(profile, all_replies)
    else:
        # No updates, but proceed_anyway is True
        generate_and_send_nudge(profile, all_replies)


if __name__ == "__main__":
    main()
