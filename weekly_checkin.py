"""
Weekly OS - Personal Operating System Check-in
================================================
Reads profile.md from Google Drive, reasons over it with Gemini,
then sends a weekly brief to Telegram and email.

New in this version:
  - Reads your Telegram messages from the past 7 days as context
  - Folds Telegram replies into profile.md before generating the weekly brief
  - Warns if your profile.md hasn't been updated in 14+ days
  - Maintains a goals_log.md on Drive with auto-compression at 30 days and 1 year
"""

import json
import os
import re
import smtplib
import sys
from email.header import Header
from email.message import EmailMessage
from calendar import month_name
from datetime import datetime, timedelta, timezone
from itertools import groupby

import google.generativeai as genai
import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload

from telegram_context import acknowledge_telegram_updates, fetch_recent_telegram_notes, summarize_reply_signals
from drive_context import build_drive_service, read_drive_file, write_drive_file
from llm_context import generate_with_fallback
import markdown

# ── Config ────────────────────────────────────────────────────────────────────

def _clean_secret(v: str) -> str:
    return v.strip().replace("\xa0", " ") if v else v

GDRIVE_FILE_ID           = _clean_secret(os.environ.get("GDRIVE_FILE_ID", ""))
GDRIVE_SA_JSON           = os.environ.get("GDRIVE_SERVICE_ACCOUNT_JSON", "")
GDRIVE_GOALS_LOG_FILE_ID = _clean_secret(os.environ.get("GDRIVE_GOALS_LOG_FILE_ID", ""))
GEMINI_API_KEY           = _clean_secret(os.environ.get("GEMINI_API_KEY", ""))
TELEGRAM_BOT_TOKEN       = _clean_secret(os.environ.get("TELEGRAM_BOT_TOKEN", ""))
TELEGRAM_CHAT_ID         = _clean_secret(str(os.environ.get("TELEGRAM_CHAT_ID", "")))
EMAIL_ADDRESS            = _clean_secret(os.environ.get("EMAIL_ADDRESS", ""))
EMAIL_PASSWORD           = _clean_secret(os.environ.get("EMAIL_PASSWORD", ""))
EMAIL_RECIPIENT          = _clean_secret(os.environ.get("EMAIL_RECIPIENT", EMAIL_ADDRESS))

NOW_UTC    = datetime.now(timezone.utc)
TODAY      = NOW_UTC.strftime("%A, %d %B %Y")
TODAY_DATE = NOW_UTC.date()

REQUIRED_VARS = [
    "GDRIVE_FILE_ID",
    "GDRIVE_SERVICE_ACCOUNT_JSON",
    "GEMINI_API_KEY",
    "EMAIL_ADDRESS",
    "EMAIL_PASSWORD",
]

def validate_env() -> None:
    """Validate that all required environment variables are present and non-empty."""
    missing = [v for v in REQUIRED_VARS if not os.environ.get(v)]
    if missing:
        print("[Weekly OS] ERROR: Missing or empty required environment variables:")
        for v in missing:
            print(f"  - {v}")
        sys.exit(1)

DRIVE_SCOPES = [
    "https://www.googleapis.com/auth/drive",
]


# ── Google Drive ──────────────────────────────────────────────────────────────

def fetch_profile(service) -> str:
    return read_drive_file(service, GDRIVE_FILE_ID)

def fetch_goals_log(service) -> str:
    if not GDRIVE_GOALS_LOG_FILE_ID:
        return ""
    try:
        return read_drive_file(service, GDRIVE_GOALS_LOG_FILE_ID)
    except Exception as e:
        print(f"[Goals Log] Fetch failed: {e}")
        return ""

def write_goals_log(service, content: str) -> None:
    if not GDRIVE_GOALS_LOG_FILE_ID:
        return
    try:
        write_drive_file(service, GDRIVE_GOALS_LOG_FILE_ID, content)
    except Exception as e:
        print(f"[Goals Log] Write failed: {e}")

def write_profile(service, content: str) -> None:
    write_drive_file(service, GDRIVE_FILE_ID, content)


# ── Staleness Check ───────────────────────────────────────────────────────────

def check_profile_staleness(profile_content: str) -> str | None:
    """Return a warning string if 'Last updated:' field is >14 days old."""
    for line in profile_content.splitlines():
        if line.strip().lower().startswith("last updated:"):
            date_str = line.split(":", 1)[1].strip()
            try:
                last_updated = datetime.strptime(date_str, "%Y-%m-%d").date()
                days_old = (TODAY_DATE - last_updated).days
                if days_old > 14:
                    return (
                        f"PROFILE STALE: Last updated {days_old} days ago ({date_str}). "
                        "Flagged sections may be outdated."
                    )
            except ValueError:
                pass
    return None


# ── Telegram ──────────────────────────────────────────────────────────────────

def fetch_telegram_replies() -> tuple[list[str], int, int]:
    """
    Fetch all pending Telegram updates. Filter to messages from TELEGRAM_CHAT_ID
    in the past 7 days. Returns (list of '[timestamp] text' strings, total_updates, max update_id).
    """
    return fetch_recent_telegram_notes(
        TELEGRAM_BOT_TOKEN,
        TELEGRAM_CHAT_ID,
        NOW_UTC,
        days=7,
        acknowledge=False,
    )


def send_telegram(message: str) -> bool:
    """Send the brief via Telegram. Splits into chunks if over 4000 chars."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    chunks = [message[i : i + 4000] for i in range(0, len(message), 4000)]
    success = True
    for chunk in chunks:
        resp = requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": chunk, "parse_mode": "Markdown"},
            timeout=15,
        )
        if not resp.ok:
            print(f"[Telegram] Send failed: {resp.status_code} {resp.text}")
            success = False
    return success


# ── Goals Log ─────────────────────────────────────────────────────────────────

def _parse_goals_log(log_content: str) -> list[dict]:
    """
    Parse goals_log.md into a list of section dicts:
      {type: 'week'|'month'|'year', date: date, label: str, content: str}
    """
    sections: list[dict] = []
    current_header: dict | None = None
    current_lines: list[str] = []

    for line in log_content.splitlines(keepends=True):
        week_m  = re.match(r"^### Week of (\d{4}-\d{2}-\d{2})", line)
        seed_m  = re.match(r"^### Initial Seed: (\d{4}-\d{2}-\d{2})", line)
        month_m = re.match(r"^## Monthly Summary: (\w+ \d{4})", line)
        year_m  = re.match(r"^## Yearly Summary: (\d{4})", line)

        if week_m or seed_m or month_m or year_m:
            if current_header is not None:
                sections.append({**current_header, "content": "".join(current_lines)})
            current_lines = [line]
            if week_m or seed_m:
                raw = (week_m or seed_m).group(1)
                from datetime import date
                d = date.fromisoformat(raw)
                current_header = {"type": "week", "date": d, "label": raw}
            elif month_m:
                label = month_m.group(1)
                parts = label.split()
                mn = list(month_name).index(parts[0])
                from datetime import date
                d = date(int(parts[1]), mn, 1)
                current_header = {"type": "month", "date": d, "label": label}
            elif year_m:
                from datetime import date
                d = date(int(year_m.group(1)), 1, 1)
                current_header = {"type": "year", "date": d, "label": year_m.group(1)}
        else:
            current_lines.append(line)

    if current_header is not None:
        sections.append({**current_header, "content": "".join(current_lines)})

    return sections


def _gemini_compress(api_key: str, entries_text: str, label: str) -> str:
    """Ask Gemini to summarise a group of log entries into one paragraph."""
    prompt = (
        f"Summarise the following goal log entries into a concise paragraph (max 5 sentences). "
        f"Focus on what was achieved, what stalled, and any notable patterns. "
        f"Be direct and factual. No headers. Label context: {label}\n\n"
        f"Entries:\n{entries_text}"
    )
    return generate_with_fallback(api_key, prompt)


def compress_goals_log(log_content: str, api_key: str) -> str:
    """
    Run tiered compression on the goals log:
      - Weekly entries older than 30 days  → Monthly Summary (via Gemini)
      - Monthly summaries older than 1 year → Yearly Summary (via Gemini)
    Returns the (possibly rewritten) log content.
    """
    if not log_content.strip():
        return log_content

    sections = _parse_goals_log(log_content)
    threshold_month = TODAY_DATE - timedelta(days=30)
    threshold_year  = TODAY_DATE - timedelta(days=365)

    # Pass 1: compress old weekly entries → monthly summaries
    old_weeks = [s for s in sections if s["type"] == "week" and s["date"] < threshold_month]
    if old_weeks:
        old_weeks.sort(key=lambda s: s["date"].strftime("%Y-%m"))
        for month_key, grp in groupby(old_weeks, key=lambda s: s["date"].strftime("%Y-%m")):
            grp_list = list(grp)
            combined = "\n".join(s["content"].strip() for s in grp_list)
            yr, mo = month_key.split("-")
            label = f"{month_name[int(mo)]} {yr}"
            summary = _gemini_compress(api_key, combined, f"Monthly Summary: {label}")
            from datetime import date
            new_sec = {
                "type": "month",
                "date": date(int(yr), int(mo), 1),
                "label": label,
                "content": f"## Monthly Summary: {label}\n{summary}\n",
            }
            for s in grp_list:
                sections.remove(s)
            sections.append(new_sec)
        print(f"[Goals Log] Compressed {len(old_weeks)} weekly entries into monthly summaries.")

    # Pass 2: compress old monthly summaries → yearly summaries
    old_months = [s for s in sections if s["type"] == "month" and s["date"] < threshold_year]
    if old_months:
        old_months.sort(key=lambda s: s["date"].strftime("%Y"))
        for yr_key, grp in groupby(old_months, key=lambda s: s["date"].strftime("%Y")):
            grp_list = list(grp)
            combined = "\n".join(s["content"].strip() for s in grp_list)
            summary = _gemini_compress(api_key, combined, f"Yearly Summary: {yr_key}")
            from datetime import date
            new_sec = {
                "type": "year",
                "date": date(int(yr_key), 1, 1),
                "label": yr_key,
                "content": f"## Yearly Summary: {yr_key}\n{summary}\n",
            }
            for s in grp_list:
                sections.remove(s)
            sections.append(new_sec)
        print(f"[Goals Log] Compressed {len(old_months)} monthly summaries into yearly summaries.")

    # Reconstruct: years → months → weeks (chronological within each tier)
    sections.sort(key=lambda s: ({"year": 0, "month": 1, "week": 2}[s["type"]], s["date"]))
    return "\n".join(s["content"].strip() for s in sections) + "\n"


def append_goals_entry(log_content: str, goal_status_section: str) -> str:
    """Append this week's goal status to the log."""
    entry = f"### Week of {TODAY_DATE.isoformat()}\n{goal_status_section.strip()}\n"
    return log_content.rstrip() + "\n\n" + entry + "\n"


def extract_goal_status_section(brief: str) -> str:
    """Extract the ### Goal Status This Week section from the generated brief (case-insensitive)."""
    # Use regex to find the section between the header and the next ## header or end of file
    pattern = r"### Goal Status This Week\s*(.*?)(?=\n##|$)"
    match = re.search(pattern, brief, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return ""


# ── Gemini ────────────────────────────────────────────────────────────────────

PROFILE_UPDATE_PROMPT = """\
You update a user's personal operating system profile markdown before the weekly brief is generated.

Inputs:
- The current profile markdown
- Telegram replies from the past week
- Deterministic interpreted signals from those replies

Return the complete updated profile markdown only. No commentary. No code fences.

Rules:
- Preserve the user's existing markdown structure and wording as much as possible.
- Treat Telegram replies as factual status updates, even if terse.
- Update "Last updated:" to {date} if it exists.
- Fold durable status into the most relevant existing section: active goals, personal goals, learning, job search, health, or notes.
- For job-search replies, record both applications submitted and any HR calls/interviews scheduled.
- For learning replies, record module progress such as finance/ML or unsupervised ML.
- For health replies, update only the specific habit mentioned.
- Do not invent numbers, companies, dates, or outcomes not present in the replies.
- Do not remove unrelated profile content.
- If no existing section fits, add a short "## Latest Weekly Updates" section near the end.
"""

SYSTEM_PROMPT = """\
You are a sharp, direct personal operating system assistant.
You receive a personal profile, today's date, any Telegram notes from the past week,
and a goal history log. Produce a concise, actionable weekly check-in brief.

Structure your response EXACTLY as follows (use these headers verbatim):

## Weekly OS Check-in | {date}

### Finance Review
- Flag any rules or thresholds worth acting on this week
- Note upcoming catalysts, dates, or conditions to watch
- 3-5 bullets max

### Health Check-in
- Assess each habit listed in the profile honestly
- Reference any health notes the user logged this week if available
- Flag habits likely to drift without attention
- 3-5 bullets max

### Personal & Goals
- Surface time-sensitive items from the goals section
- Reference goal history trends if visible
- Reference any goal-related notes logged this week
- 3-5 bullets max

### This Week's Focus
2-3 sentences. One clear priority. One thing to protect. One thing to let go of.

### Goal Status This Week
For each active goal in the profile, one line exactly:
- [Goal name]: [status or observation]
Reference user's logged notes if relevant. If no update, say "no update".

### Profile Freshness
List sections that look stale or may need updating. Gentle nudges, not instructions.

---
Rules:
- Be direct. No filler like "Great job" or "Remember to".
- No em dashes. Use plain dashes or colons.
- Reference specific numbers or rules from the profile when relevant.
- Treat Telegram notes as ground-truth status updates, even if terse.
- Do not ask whether a goal was started if the notes already show progress.
- If the user reports applications, HR calls, interviews, or a module starting, reflect that as current pipeline/learning status.
- If a section has nothing actionable, say so in one line and move on.
"""


def _strip_markdown_fence(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:markdown|md)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip() + "\n"


def _looks_like_profile_update(original: str, updated: str) -> bool:
    if not updated.strip():
        return False
    if len(updated) < max(200, int(len(original) * 0.5)):
        return False
    original_headers = re.findall(r"^#{1,3}\s+.+$", original, flags=re.MULTILINE)
    kept_headers = sum(1 for header in original_headers if header in updated)
    if original_headers and kept_headers < max(1, len(original_headers) // 2):
        return False
    return True


def update_profile_from_replies(
    profile_content: str,
    replies: list[str],
    api_key: str,
) -> str:
    """Fold weekly Telegram replies into profile.md before generating the brief."""
    if not replies:
        return profile_content

    reply_signals = summarize_reply_signals(replies)
    parts = [
        f"Today is {TODAY}.",
        "",
        "## Current Profile Markdown",
        profile_content,
    ]
    if reply_signals:
        parts += ["", "## Interpreted Reply Signals", *[f"- {s}" for s in reply_signals]]
    parts += ["", "## Telegram Replies This Week", *replies]

    prompt_text = "\n".join(parts)
    sys_prompt = PROFILE_UPDATE_PROMPT.replace("{date}", TODAY_DATE.isoformat())

    try:
        response_text = generate_with_fallback(api_key, prompt_text, sys_prompt)
        updated = _strip_markdown_fence(response_text)
        if _looks_like_profile_update(profile_content, updated):
            return updated
        raise ValueError("profile update did not look like a complete profile markdown")
    except Exception as e:
        raise RuntimeError(f"All Gemini profile update models failed. Last error: {e}")


def generate_brief(
    profile_content: str,
    replies: list[str],
    goals_log: str,
    staleness_warning: str | None,
    api_key: str,
) -> str:
    """Build the full prompt context and call Gemini with fallback models."""
    parts = [f"Today is {TODAY}.", "", "## Profile", profile_content]

    if staleness_warning:
        parts += ["", f"**{staleness_warning}**"]

    if replies:
        reply_signals = summarize_reply_signals(replies)
        if reply_signals:
            parts += ["", "## Interpreted Signals From Telegram", *[f"- {s}" for s in reply_signals]]
        parts += [
            "",
            "## Your Notes This Week (Telegram)",
            "Use these as factual status updates. Do not ignore terse confirmations like 'yup' or 'applied more'.",
            *replies,
        ]
    else:
        parts += ["", "## Your Notes This Week", "No notes logged this week."]

    if goals_log:
        parts += ["", "## Goal History Log", goals_log]

    prompt_text = "\n".join(parts)
    sys_prompt = SYSTEM_PROMPT.replace("{date}", TODAY)

    try:
        return generate_with_fallback(api_key, prompt_text, sys_prompt)
    except Exception as e:
        raise RuntimeError(f"All Gemini models failed. Last error: {e}")


# ── Email ─────────────────────────────────────────────────────────────────────

def markdown_to_html(text: str) -> str:
    body = markdown.markdown(text, extensions=['tables', 'fenced_code'])
    return (
        "<html><head><style>\n"
        "body { font-family: Arial, sans-serif; max-width: 640px; margin: auto; padding: 24px; color: #222; }\n"
        "h2 { color: #2c3e50; border-bottom: 2px solid #eee; padding-bottom: 6px; }\n"
        "h3 { color: #34495e; margin-top: 20px; }\n"
        "li { margin-bottom: 6px; }\n"
        "hr { border: none; border-top: 1px solid #eee; margin: 20px 0; }\n"
        "p { margin: 12px 0; line-height: 1.5; }\n"
        "pre, code { background-color: #f8f9fa; padding: 2px 4px; border-radius: 4px; }\n"
        "pre { padding: 12px; overflow-x: auto; }\n"
        "</style></head><body>\n"
        f"{body}\n"
        f"<br><p style='color:#aaa;font-size:12px'>Weekly OS - Generated {TODAY}</p>\n"
        "</body></html>"
    )


def send_email(brief: str) -> bool:
    try:
        # Super-clean: Replace ALL non-ASCII whitespace (like \xa0, \u2007, etc) with regular spaces
        # This regex matches any whitespace that is NOT a standard space/newline and replaces it
        def robust_clean(text: str) -> str:
            # Replace common non-breaking spaces and other weirdness
            cleaned = text.replace("\xa0", " ").replace("\u2007", " ").replace("\u202f", " ")
            # Also catch any other non-ascii whitespace
            return re.sub(r'[^\x00-\x7F]+', lambda m: m.group(0).replace('\xa0', ' '), cleaned)

        clean_brief = robust_clean(brief)
        clean_subject = robust_clean(f"Weekly OS Check-in | {TODAY}")
        
        sender = robust_clean(EMAIL_ADDRESS.strip())
        recipient = robust_clean(EMAIL_RECIPIENT.strip())

        msg = EmailMessage()
        msg["Subject"] = clean_subject
        msg["From"] = sender
        msg["To"] = recipient

        # Set the plain text body
        msg.set_content(clean_brief)

        # Add the HTML version
        msg.add_alternative(markdown_to_html(clean_brief), subtype="html")

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender, EMAIL_PASSWORD)
            # Use raw sendmail with bytes to avoid any internal string-to-ascii conversions
            server.sendmail(sender, [recipient], msg.as_bytes())
        return True
    except Exception as e:
        print(f"[Email] Failed: {e}")
        # Final desperate attempt: strip all non-ascii and try once more
        try:
            print("      Attempting ASCII-only fallback...")
            sender = "".join(c for c in EMAIL_ADDRESS if ord(c) < 128)
            recipient = "".join(c for c in EMAIL_RECIPIENT if ord(c) < 128)
            safe_brief = "".join(c for c in brief if ord(c) < 128)
            safe_subject = "".join(c for c in f"Weekly OS Check-in | {TODAY}" if ord(c) < 128)
            
            msg = EmailMessage()
            msg["Subject"] = safe_subject
            msg["From"] = sender
            msg["To"] = recipient
            msg.set_content(safe_brief)
            
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.login(sender, EMAIL_PASSWORD)
                server.send_message(msg)
            return True
        except Exception as e2:
            print(f"      Desperate fallback also failed: {e2}")
            return False


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    validate_env()
    print(f"[Weekly OS] Starting check-in for {TODAY}")

    # Step 0: Read Telegram replies from the past 7 days
    replies = []
    total_updates = 0
    max_update_id = 0
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        print("\n[0/7] Reading Telegram replies from the past 7 days...")
        replies, total_updates, max_update_id = fetch_telegram_replies()
        print(f"      {len(replies)} relevant message(s) found.")
        if total_updates > len(replies):
            print(f"      (Note: {total_updates - len(replies)} update(s) were skipped/filtered).")
    else:
        print("\n[0/7] Skipping Telegram replies (TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set).")

    # Step 1: Connect to Drive
    print("\n[1/7] Connecting to Google Drive...")
    drive = build_drive_service(GDRIVE_SA_JSON, DRIVE_SCOPES)

    # Step 2: Fetch profile
    print("\n[2/7] Fetching profile.md...")
    profile = fetch_profile(drive)
    print(f"      Loaded ({len(profile)} chars).")

    # Step 3: Fold Telegram replies into profile.md before generating the brief
    print("\n[3/8] Updating profile.md from Telegram replies...")
    if replies:
        updated_profile = update_profile_from_replies(profile, replies, GEMINI_API_KEY)
        if updated_profile != profile:
            write_profile(drive, updated_profile)
            profile = updated_profile
            print(f"      Profile updated and saved ({len(profile)} chars).")
        else:
            print("      No profile changes needed.")
    else:
        print("      No replies to fold into profile.")

    # Step 4: Staleness check
    print("\n[4/8] Checking profile freshness...")
    staleness_warning = check_profile_staleness(profile)
    if staleness_warning:
        print(f"      Warning: {staleness_warning}")
    else:
        print("      Profile is current.")

    # Step 5: Fetch goals log
    print("\n[5/8] Fetching goals log...")
    goals_log = fetch_goals_log(drive)
    print(f"      Loaded ({len(goals_log)} chars).")

    # Step 6: Compress goals log if needed
    print("\n[6/8] Compressing goals log (if entries are old enough)...")
    try:
        goals_log = compress_goals_log(goals_log, GEMINI_API_KEY)
        compressed_ok = True
    except Exception as e:
        print(f"      Compression failed: {e}")

    # Step 7: Generate brief
    print("\n[7/8] Generating brief with Gemini...")
    brief = generate_brief(profile, replies, goals_log, staleness_warning, GEMINI_API_KEY)
    print(f"      Brief generated ({len(brief)} chars).")

    # Step 8: Deliver
    print("\n[8/8] Sending brief...")
    tg_ok = False
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        tg_ok = send_telegram(brief)
        print(f"      Telegram: {'OK' if tg_ok else 'FAILED'}")
    else:
        print("      Telegram: SKIPPED (not configured)")

    email_ok = send_email(brief)
    print(f"      Email:    {'OK' if email_ok else 'FAILED'}")

    if not tg_ok and not email_ok and (TELEGRAM_BOT_TOKEN or EMAIL_ADDRESS):
        # Only raise if at least one channel was configured but failed
        if not email_ok:
            raise RuntimeError("Email delivery failed.")

    # Post-send: acknowledge Telegram updates + append to goals log
    if max_update_id > 0:
        acknowledge_telegram_updates(TELEGRAM_BOT_TOKEN, max_update_id)

    goal_status = extract_goal_status_section(brief)
    if not GDRIVE_GOALS_LOG_FILE_ID:
        print("\n[!] Skipping goals log update: GDRIVE_GOALS_LOG_FILE_ID not configured.")
    elif not goal_status:
        print("\n[!] Skipping goals log update: '### Goal Status This Week' section not found in brief.")
        # Debug: Print first few lines of brief to see headers
        print("    Brief starts with:")
        for line in brief.splitlines()[:10]:
            print(f"      {line}")
    else:
        print("\n[+] Appending goal status to goals log...")
        updated_log = append_goals_entry(goals_log, goal_status)
        write_goals_log(drive, updated_log)
        print("    Goals log updated.")

    print(f"\n[Weekly OS] Done.")


if __name__ == "__main__":
    main()
