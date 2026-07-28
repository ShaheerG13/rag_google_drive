# Step 2: connect to Google Drive (via OAuth) and list files in our target folder.

import os
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

load_dotenv()

# Read-only access to Drive — we can look but never change anything.
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

FOLDER_ID = os.environ["DRIVE_FOLDER_ID"]


def get_drive_service():
    creds = None

    # token.json holds your saved login from last time, if it exists.
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)

    # If we have no valid login, get one.
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            # Token expired but is renewable — refresh it silently.
            creds.refresh(Request())
        else:
            # First time: open a browser for you to log in and approve.
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)
        # Save the login so future runs don't prompt again.
        with open("token.json", "w") as f:
            f.write(creds.to_json())

    return build("drive", "v3", credentials=creds)


def list_files(service):
    query = f"'{FOLDER_ID}' in parents and trashed = false"

    files = []
    page_token = None

    # Drive pages results, so keep asking until there's no next page.
    while True:
        results = service.files().list(
            q=query,
            # md5Checksum lets us skip unchanged files later. Google-native files
            # (Docs/Sheets/Slides) don't have one — we fall back to modifiedTime.
            fields="nextPageToken, files(id, name, mimeType, modifiedTime, md5Checksum, webViewLink)",
            pageSize=100,
            pageToken=page_token,
        ).execute()

        files.extend(results.get("files", []))

        page_token = results.get("nextPageToken")
        if not page_token:
            return files


# Google-native files must be converted ("exported") to a text format.
# Caps out at 10 MB per file.
def export_file(service, file_id, mime_type):
    return service.files().export(fileId=file_id, mimeType=mime_type).execute()


# Regular uploads (PDF, .docx, .txt) download as raw bytes.
def download_file(service, file_id):
    return service.files().get_media(fileId=file_id).execute()


if __name__ == "__main__":
    service = get_drive_service()
    files = list_files(service)

    print(f"Found {len(files)} file(s) in the folder:\n")
    for f in files:
        print(f"- {f['name']}  ({f['mimeType']})")
