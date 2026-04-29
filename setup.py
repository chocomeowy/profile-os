"""
Profile OS - First-Time Setup
==============================
Run this once to initialise the project for yourself or anyone forking the repo.

What it does:
  1. Validates all required environment variables
  2. Creates goals_log.md on Google Drive (owned by the service account)
  3. Seeds it with your current goal status (via Gemini reading your profile.md)
  4. Shares goals_log.md with your personal email so you can view it on Drive
  5. Drains any existing Telegram update backlog (so the first weekly run starts clean)
  6. Prints the GDRIVE_GOALS_LOG_FILE_ID value to add as a GitHub Secret

Run:
  export GDRIVE_FILE_ID="..."
  export GDRIVE_SERVICE_ACCOUNT_JSON="$(cat your-service-account.json)"
  export GEMINI_API_KEY="..."
  export TELEGRAM_BOT_TOKEN="..."
  export TELEGRAM_CHAT_ID="..."
  export OWNER_EMAIL="your@gmail.com"   # optional: share goals_log.md with yourself
  python setup.py
"""

import json
import os
import sys

import google.generativeai as genai
import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload

from datetime import datetime, timezone

TODAY_DATE = datetime.now(timezone.utc).strftime("%Y-%m-%d")

DRIVE_SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive.readonly",
]

REQUIRED_VARS = [
    "GDRIVE_FILE_ID",
    "GDRIVE_SERVICE_ACCOUNT_JSON",
    "GEMINI_API_KEY",
]

OPTIONAL_VARS = [
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
]


# ── Validation ────────────────────────────────────────────────────────────────

def validate_env() -> None:
    missing = [v for v in REQUIRED_VARS if not os.environ.get(v)]
    if missing:
        print("[Setup] ERROR: Missing required environment variables:")
        for v in missing:
            print(f"  - {v}")
        sys.exit(1)
    print("[Setup] All required environment variables present.")


# ── Drive ─────────────────────────────────────────────────────────────────────

def build_drive_service():
    sa_info = json.loads(os.environ["GDRIVE_SERVICE_ACCOUNT_JSON"])
    creds = service_account.Credentials.from_service_account_info(
        sa_info, scopes=DRIVE_SCOPES
    )
    return build("drive", "v3", credentials=creds)


def _read_file(service, file_id: str) -> str:
    """Read a file's content from Drive. Handles both regular files and Google Docs."""
    if not file_id or not file_id.strip():
        raise ValueError("Google Drive file ID is missing or empty.")

    file_metadata = service.files().get(fileId=file_id, fields="mimeType").execute()
    mime_type = file_metadata.get("mimeType", "")

    if mime_type.startswith("application/vnd.google-apps."):
        request = service.files().export_media(fileId=file_id, mimeType="text/plain")
    else:
        request = service.files().get_media(fileId=file_id)

    content = request.execute()
    return content.decode("utf-8") if isinstance(content, bytes) else content


def fetch_profile(service) -> str:
    return _read_file(service, os.environ["GDRIVE_FILE_ID"])


def create_drive_file(service, name: str, content: str) -> str:
    """Create a new text file on Drive owned by the service account. Returns file ID."""
    media = MediaInMemoryUpload(content.encode("utf-8"), mimetype="text/plain")
    file_meta = {"name": name, "mimeType": "text/plain"}
    result = service.files().create(
        body=file_meta, media_body=media, fields="id"
    ).execute()
    return result["id"]


def share_file_with_user(service, file_id: str, email: str) -> None:
    """Share a Drive file with a user email (viewer access)."""
    permission = {"type": "user", "role": "writer", "emailAddress": email}
    service.permissions().create(
        fileId=file_id, body=permission, sendNotificationEmail=False
    ).execute()


# ── Seed goals log ────────────────────────────────────────────────────────────

SEED_PROMPT = """
You are reading a personal profile that contains Active Goals.
Extract each goal and write a brief one-line status entry for each.
Format each as a bullet: - [Goal name]: [current status based on the profile]
Be factual and specific. Do not add any headers or extra text - just the bullet list.
"""


def seed_goals_log(profile_content: str) -> str:
    """Use Gemini to extract current goals from the profile and create the seed entry."""
    genai.configure(api_key=os.environ["GEMINI_API_KEY"])
    model = genai.GenerativeModel(
        model_name="gemini-1.5-flash",
        system_instruction=SEED_PROMPT,
    )
    response = model.generate_content(
        f"Here is my profile:\n\n{profile_content}"
    )
    goal_bullets = response.text.strip()
    return f"### Initial Seed: {TODAY_DATE}\n{goal_bullets}\n"


# ── Telegram ──────────────────────────────────────────────────────────────────

def drain_telegram_updates() -> int:
    """
    Drain all pending Telegram updates so the first weekly run starts clean.
    Returns the number of updates drained.
    """
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    url = f"https://api.telegram.org/bot{token}/getUpdates"

    # Fetch all pending updates
    resp = requests.get(url, params={"timeout": 0, "limit": 100}, timeout=15)
    data = resp.json()
    updates = data.get("result", [])

    if not updates:
        return 0

    max_id = max(u["update_id"] for u in updates)

    # Acknowledge all by advancing offset past the last update
    requests.get(url, params={"offset": max_id + 1, "timeout": 0}, timeout=15)
    return len(updates)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("\n=== Profile OS Setup ===\n")

    # 1. Validate
    validate_env()

    # 2. Connect to Drive
    print("\n[1/5] Connecting to Google Drive...")
    drive = build_drive_service()
    print("      Connected.")

    # 3. Fetch profile
    print("\n[2/5] Fetching profile.md from Drive...")
    profile = fetch_profile(drive)
    print(f"      Loaded ({len(profile)} chars).")

    # 4. Seed goals log
    print("\n[3/5] Generating initial goals log seed with Gemini...")
    seed_content = seed_goals_log(profile)
    print("      Seed generated:")
    print("      " + "\n      ".join(seed_content.splitlines()))

    # 5. Create goals_log.md on Drive
    print("\n[4/5] Creating goals_log.md on Google Drive...")
    goals_log_id = create_drive_file(drive, "goals_log.md", seed_content)
    print(f"      Created. File ID: {goals_log_id}")

    # Share with owner if email provided
    owner_email = os.environ.get("OWNER_EMAIL", "").strip()
    if owner_email:
        share_file_with_user(drive, goals_log_id, owner_email)
        print(f"      Shared with {owner_email} (writer access).")
    else:
        print("      OWNER_EMAIL not set - file not shared with a personal account.")
        print("      You can find it in your service account's Drive or share it manually.")

    # 6. Drain Telegram backlog
    print("\n[5/5] Draining existing Telegram update backlog...")
    if os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
        drained = drain_telegram_updates()
        print(f"      Drained {drained} pending update(s).")
    else:
        print("      Skipping: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set.")

    # 7. Done - print what to do next
    print("\n=== Setup Complete ===\n")
    print("Add this as a GitHub Secret in your repo:")
    print(f"  GDRIVE_GOALS_LOG_FILE_ID = {goals_log_id}\n")
    print("Checklist:")
    print("  [x] goals_log.md created on Drive and seeded")
    print("  [x] Telegram backlog cleared")
    print("  [ ] Add GDRIVE_GOALS_LOG_FILE_ID to GitHub Secrets")
    if not owner_email:
        print("  [ ] Set OWNER_EMAIL and re-run if you want to share goals_log.md with yourself")
    print("  [ ] Confirm weekly.yml is at .github/workflows/weekly.yml in your repo")
    print("  [ ] Trigger the workflow manually via Actions > Weekly OS Check-in > Run workflow\n")


if __name__ == "__main__":
    main()
