"""
Step 2: connect to Google Drive and list the files in our target folder.

Run it with:  ./venv/Scripts/python.exe drive.py
"""

import os
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build

load_dotenv()

# Read-only access to Drive — the robot can look but never change anything.
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

FOLDER_ID = os.environ["DRIVE_FOLDER_ID"]


def get_drive_service():
    """Log in as the service account and return a Drive API client."""
    creds = service_account.Credentials.from_service_account_file(
        "service_account.json", scopes=SCOPES
    )
    # "drive", "v3" = version 3 of the Drive API.
    return build("drive", "v3", credentials=creds)


def list_files(service):
    """Return every non-trashed file directly inside our folder."""
    # This query means: parent folder is FOLDER_ID, and the file isn't in the trash.
    query = f"'{FOLDER_ID}' in parents and trashed = false"

    results = service.files().list(
        q=query,
        # Only ask for the fields we care about (keeps responses small).
        fields="files(id, name, mimeType, modifiedTime)",
    ).execute()

    return results.get("files", [])


if __name__ == "__main__":
    service = get_drive_service()
    files = list_files(service)

    print(f"Found {len(files)} file(s) in the folder:\n")
    for f in files:
        print(f"- {f['name']}  ({f['mimeType']})")
