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
