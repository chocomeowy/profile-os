"""
Shared Google Drive helpers for the OS scripts.
"""

import json
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload

def build_drive_service(sa_json: str, scopes: list[str]):
    """Build and return an authenticated Google Drive service client."""
    sa_info = json.loads(sa_json)
    creds = service_account.Credentials.from_service_account_info(
        sa_info, scopes=scopes
    )
    return build("drive", "v3", credentials=creds)

def read_drive_file(service, file_id: str) -> str:
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

def write_drive_file(service, file_id: str, content: str) -> None:
    """Update an existing file on Google Drive."""
    media = MediaInMemoryUpload(content.encode("utf-8"), mimetype="text/plain")
    service.files().update(fileId=file_id, media_body=media).execute()


def get_or_create_inbox_file_id(service, profile_file_id: str, owner_email: str = None) -> str:
    """Find or create telegram_inbox.json on Google Drive inside the parent folder of profile.md."""
    try:
        # Optimized list search to support shared folders and Shared Drives
        results = service.files().list(
            q="name = 'telegram_inbox.json' and trashed = false",
            spaces="drive",
            fields="files(id, name)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        files = results.get("files", [])
        if files:
            return files[0]["id"]
    except Exception as e:
        print(f"[Drive Inbox] Search failed: {e}")

    # Fetch parent folder of profile.md to bypass Service Account storage quota limit
    parent_id = None
    if profile_file_id:
        try:
            profile_metadata = service.files().get(fileId=profile_file_id, fields="parents").execute()
            parents = profile_metadata.get("parents", [])
            if parents:
                parent_id = parents[0]
                print(f"[Drive Inbox] Found profile.md parent folder: {parent_id}")
        except Exception as pe:
            print(f"[Drive Inbox] Could not fetch profile parents: {pe}")

    # Create new file if not found
    file_metadata = {
        "name": "telegram_inbox.json",
        "mimeType": "application/json",
    }
    if parent_id:
        file_metadata["parents"] = [parent_id]

    media = MediaInMemoryUpload(b"[]", mimetype="application/json")
    try:
        file = service.files().create(
            body=file_metadata,
            media_body=media,
            fields="id",
            supportsAllDrives=True
        ).execute()
        file_id = file.get("id")
        print(f"[Drive Inbox] Created 'telegram_inbox.json' inside parent folder. File ID: {file_id}")
        
        # Share it if owner email is provided
        if owner_email and owner_email.strip():
            try:
                permission = {"type": "user", "role": "writer", "emailAddress": owner_email.strip()}
                service.permissions().create(
                    fileId=file_id, body=permission, sendNotificationEmail=False
                ).execute()
                print(f"[Drive Inbox] Shared with {owner_email.strip()}")
            except Exception as se:
                print(f"[Drive Inbox] Could not share file: {se}")
                
        return file_id
    except Exception as e:
        # Extract Service Account email for helpful error message
        sa_email = "your service account's email address"
        try:
            import os
            sa_json = os.environ.get("GDRIVE_SERVICE_ACCOUNT_JSON")
            if sa_json:
                import json
                sa_info = json.loads(sa_json)
                sa_email = sa_info.get("client_email", sa_email)
        except Exception:
            pass

        print("\n" + "="*80)
        print("[Drive Inbox] ERROR: Google Service Accounts have a 0GB personal storage quota limit.")
        print("Because the file 'telegram_inbox.json' does not exist yet, the service account tried")
        print("to create it, which failed with a storageQuotaExceeded error.")
        print("\nTo fix this immediately, please follow these steps:")
        print("1. Open Google Drive in your web browser.")
        print("2. Go to the parent folder of your 'profile.md'.")
        print("3. Create a new text file named 'telegram_inbox.json' inside that folder.")
        print("4. Edit the file and write an empty JSON array: []")
        print(f"5. Ensure this file is shared with your Service Account email: {sa_email}")
        print("   with 'Editor' or 'Writer' permissions (if not already shared via the parent folder).")
        print("="*80 + "\n")

        print(f"[Drive Inbox] Creation failed: {e}")
        raise e


def load_inbox_messages(service, inbox_file_id: str) -> list[str]:
    """Load messages from the Google Drive telegram inbox."""
    try:
        content = read_drive_file(service, inbox_file_id)
        if not content.strip():
            return []
        return json.loads(content)
    except Exception as e:
        print(f"[Drive Inbox] Load failed: {e}")
        return []


def save_inbox_messages(service, inbox_file_id: str, messages: list[str]) -> None:
    """Save messages to the Google Drive telegram inbox."""
    content = json.dumps(messages, indent=2)
    write_drive_file(service, inbox_file_id, content)

