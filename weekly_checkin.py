"""
Weekly OS - Personal Operating System Check-in
================================================
Reads profile.md from Google Drive, reasons over it with Gemini,
then sends a weekly brief to Telegram and email.

New in this version:
  - Reads your Telegram messages from the past 7 days as context
  - Warns if your profile.md hasn't been updated in 14+ days
  - Maintains a goals_log.md on Drive with auto-compression at 30 days and 1 year
"""

import json
import os
import re
import smtplib
from calendar import month_name
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from itertools import groupby

import google.generativeai as genai
import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload


# ── Config ────────────────────────────────────────────────────────────────────

GDRIVE_FILE_ID           = os.environ["GDRIVE_FILE_ID"]
GDRIVE_SA_JSON           = os.environ["GDRIVE_SERVICE_ACCOUNT_JSON"]
GDRIVE_GOALS_LOG_FILE_ID = os.environ.get("GDRIVE_GOALS_LOG_FILE_ID", "")
GEMINI_API_KEY           = os.environ["GEMINI_API_KEY"]
TELEGRAM_BOT_TOKEN       = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID         = str(os.environ.get("TELEGRAM_CHAT_ID", ""))
EMAIL_ADDRESS            = os.environ["EMAIL_ADDRESS"]
EMAIL_PASSWORD           = os.environ["EMAIL_PASSWORD"]
EMAIL_RECIPIENT          = os.environ.get("EMAIL_RECIPIENT", EMAIL_ADDRESS)

NOW_UTC    = datetime.now(timezone.utc)
TODAY      = NOW_UTC.strftime("%A, %d %B %Y")
TODAY_DATE = NOW_UTC.date()

DRIVE_SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/drive.file",
]


# ── Google Drive ──────────────────────────────────────────────────────────────

def build_drive_service():
    """Build and return an authenticated Google Drive service client."""
    sa_info = json.loads(GDRIVE_SA_JSON)
    creds = service_account.Credentials.from_service_account_info(
        sa_info, scopes=DRIVE_SCOPES
    )
    return build("drive", "v3", credentials=creds)


def _read_file(service, file_id: str) -> str:
    """Read a file's content from Drive. Handles both regular files and Google Docs."""
    # First, check the mimeType
    file_metadata = service.files().get(fileId=file_id, fields="mimeType").execute()
    mime_type = file_metadata.get("mimeType", "")

    if mime_type.startswith("application/vnd.google-apps."):
        # It's a Google Doc/Sheet/etc. - we must export it
        request = service.files().export_media(fileId=file_id, mimeType="text/plain")
    else:
        # It's a regular file - download directly
        request = service.files().get_media(fileId=file_id)

    content = request.execute()
    return content.decode("utf-8") if isinstance(content, bytes) else content


def _write_file(service, file_id: str, content: str) -> None:
    media = MediaInMemoryUpload(content.encode("utf-8"), mimetype="text/plain")
    service.files().update(fileId=file_id, media_body=media).execute()


def fetch_profile(service) -> str:
    return _read_file(service, GDRIVE_FILE_ID)


def fetch_goals_log(service) -> str:
    if not GDRIVE_GOALS_LOG_FILE_ID:
        return ""
    try:
        return _read_file(service, GDRIVE_GOALS_LOG_FILE_ID)
    except Exception as e:
        print(f"[Goals Log] Fetch failed: {e}")
        return ""


def write_goals_log(service, content: str) -> None:
    if not GDRIVE_GOALS_LOG_FILE_ID:
        return
    try:
        _write_file(service, GDRIVE_GOALS_LOG_FILE_ID, content)
    except Exception as e:
        print(f"[Goals Log] Write failed: {e}")


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

def fetch_telegram_replies() -> tuple[list[str], int]:
    """
    Fetch all pending Telegram updates. Filter to messages from TELEGRAM_CHAT_ID
    in the past 7 days. Returns (list of '[timestamp] text' strings, max update_id).
    """
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    cutoff = NOW_UTC - timedelta(days=7)
    messages: list[str] = []
    max_update_id = 0
    offset = 0

    while True:
        params: dict = {"timeout": 0, "limit": 100}
        if offset:
            params["offset"] = offset
        try:
            resp = requests.get(url, params=params, timeout=15)
            data = resp.json()
        except Exception as e:
            print(f"[Telegram] getUpdates error: {e}")
            break

        if not data.get("ok") or not data.get("result"):
            break

        updates = data["result"]
        for update in updates:
            uid = update["update_id"]
            max_update_id = max(max_update_id, uid)

            msg = update.get("message") or update.get("edited_message")
            if not msg:
                continue
            if str(msg.get("chat", {}).get("id", "")) != TELEGRAM_CHAT_ID:
                continue

            msg_dt = datetime.fromtimestamp(msg["date"], tz=timezone.utc)
            if msg_dt < cutoff:
                continue

            text = msg.get("text", "").strip()
            if text:
                ts = msg_dt.strftime("%Y-%m-%d %H:%M")
                messages.append(f"[{ts}] {text}")

        if len(updates) < 100:
            break
        offset = max_update_id + 1

    return messages, max_update_id


def acknowledge_telegram_updates(max_update_id: int) -> None:
    """Advance the Telegram offset to clear all processed updates."""
    if max_update_id == 0:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    try:
        requests.get(
            url, params={"offset": max_update_id + 1, "timeout": 0}, timeout=15
        )
        print(f"[Telegram] Acknowledged updates up to {max_update_id}.")
    except Exception as e:
        print(f"[Telegram] Acknowledge failed: {e}")


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


def _gemini_compress(model, entries_text: str, label: str) -> str:
    """Ask Gemini to summarise a group of log entries into one paragraph."""
    prompt = (
        f"Summarise the following goal log entries into a concise paragraph (max 5 sentences). "
        f"Focus on what was achieved, what stalled, and any notable patterns. "
        f"Be direct and factual. No headers. Label context: {label}\n\n"
        f"Entries:\n{entries_text}"
    )
    return model.generate_content(prompt).text.strip()


def compress_goals_log(log_content: str, model) -> str:
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
            summary = _gemini_compress(model, combined, f"Monthly Summary: {label}")
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
            summary = _gemini_compress(model, combined, f"Yearly Summary: {yr_key}")
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
    """Extract the ### Goal Status This Week section from the generated brief."""
    lines = brief.splitlines()
    in_section = False
    result: list[str] = []
    for line in lines:
        if line.strip().startswith("### Goal Status This Week"):
            in_section = True
            continue
        if in_section:
            if line.startswith("##"):
                break
            result.append(line)
    return "\n".join(result).strip()


# ── Gemini ────────────────────────────────────────────────────────────────────

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
- If a section has nothing actionable, say so in one line and move on.
"""


def generate_brief(
    profile_content: str,
    replies: list[str],
    goals_log: str,
    staleness_warning: str | None,
    model,
) -> str:
    """Build the full prompt context and call Gemini."""
    parts = [f"Today is {TODAY}.", "", "## Profile", profile_content]

    if staleness_warning:
        parts += ["", f"**{staleness_warning}**"]

    if replies:
        parts += ["", "## Your Notes This Week (Telegram)", *replies]
    else:
        parts += ["", "## Your Notes This Week", "No notes logged this week."]

    if goals_log:
        parts += ["", "## Goal History Log", goals_log]

    response = model.generate_content("\n".join(parts))
    return response.text


# ── Email ─────────────────────────────────────────────────────────────────────

def markdown_to_html(text: str) -> str:
    lines = text.split("\n")
    html_lines = []
    for line in lines:
        if line.startswith("## "):
            html_lines.append(
                f"<h2 style='color:#2c3e50;border-bottom:2px solid #eee;"
                f"padding-bottom:6px'>{line[3:]}</h2>"
            )
        elif line.startswith("### "):
            html_lines.append(
                f"<h3 style='color:#34495e;margin-top:20px'>{line[4:]}</h3>"
            )
        elif line.startswith("- "):
            html_lines.append(f"<li style='margin-bottom:4px'>{line[2:]}</li>")
        elif line.startswith("---"):
            html_lines.append(
                "<hr style='border:none;border-top:1px solid #eee;margin:20px 0'>"
            )
        elif line.strip() == "":
            html_lines.append("<br>")
        else:
            html_lines.append(f"<p style='margin:4px 0'>{line}</p>")

    body = "\n".join(html_lines)
    return (
        "<html><body style='font-family:Arial,sans-serif;max-width:640px;"
        f"margin:auto;padding:24px;color:#222'>\n{body}\n"
        f"<br><p style='color:#aaa;font-size:12px'>Weekly OS - Generated {TODAY}</p>"
        "</body></html>"
    )


def send_email(brief: str) -> bool:
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"Weekly OS Check-in | {TODAY}"
        msg["From"]    = EMAIL_ADDRESS
        msg["To"]      = EMAIL_RECIPIENT
        msg.attach(MIMEText(brief, "plain"))
        msg.attach(MIMEText(markdown_to_html(brief), "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
            server.sendmail(EMAIL_ADDRESS, EMAIL_RECIPIENT, msg.as_string())
        return True
    except Exception as e:
        print(f"[Email] Failed: {e}")
        return False


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"[Weekly OS] Starting check-in for {TODAY}")

    # Step 0: Read Telegram replies from the past 7 days
    replies = []
    max_update_id = 0
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        print("\n[0/7] Reading Telegram replies from the past 7 days...")
        replies, max_update_id = fetch_telegram_replies()
        print(f"      {len(replies)} message(s) found.")
    else:
        print("\n[0/7] Skipping Telegram replies (TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set).")

    # Step 1: Connect to Drive
    print("\n[1/7] Connecting to Google Drive...")
    drive = build_drive_service()

    # Step 2: Fetch profile
    print("\n[2/7] Fetching profile.md...")
    profile = fetch_profile(drive)
    print(f"      Loaded ({len(profile)} chars).")

    # Step 3: Staleness check
    print("\n[3/7] Checking profile freshness...")
    staleness_warning = check_profile_staleness(profile)
    if staleness_warning:
        print(f"      Warning: {staleness_warning}")
    else:
        print("      Profile is current.")

    # Step 4: Fetch goals log
    print("\n[4/7] Fetching goals log...")
    goals_log = fetch_goals_log(drive)
    print(f"      Loaded ({len(goals_log)} chars).")

    # Step 5: Configure Gemini
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel(
        model_name="gemini-1.5-flash",
        system_instruction=SYSTEM_PROMPT.replace("{date}", TODAY),
    )

    # Step 5a: Compress goals log if needed
    print("\n[5/7] Compressing goals log (if entries are old enough)...")
    goals_log = compress_goals_log(goals_log, model)

    # Step 6: Generate brief
    print("\n[6/7] Generating brief with Gemini...")
    brief = generate_brief(profile, replies, goals_log, staleness_warning, model)
    print(f"      Brief generated ({len(brief)} chars).")

    # Step 7: Deliver
    print("\n[7/7] Sending brief...")
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
    acknowledge_telegram_updates(max_update_id)

    goal_status = extract_goal_status_section(brief)
    if goal_status and GDRIVE_GOALS_LOG_FILE_ID:
        print("\n[+] Appending goal status to goals log...")
        updated_log = append_goals_entry(goals_log, goal_status)
        write_goals_log(drive, updated_log)
        print("    Goals log updated.")

    print(f"\n[Weekly OS] Done.")


if __name__ == "__main__":
    main()
