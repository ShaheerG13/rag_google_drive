# Step 6: keep the index in sync with Drive automatically
#
# Drive POSTs to our webhook the moment something changes. The webhook is only a
# nudge — it never says what changed, so we call changes.list to find out.
#
# Register the watch channel with:
#   ./venv/Scripts/python.exe sync.py watch https://your-tunnel.ngrok-free.app
# Run a one-off catch-up with:
#   ./venv/Scripts/python.exe sync.py poll

import os
import secrets
import sys
import threading
import time

from dotenv import load_dotenv

import drive
import ingest
import store

load_dotenv()

FOLDER_ID = os.environ["DRIVE_FOLDER_ID"]

# Drive caps how long a channel lives. We ask for a week and renew daily, well
# before expiry — Drive may hand back a shorter expiration than we requested.
CHANNEL_LIFETIME_MS = 7 * 24 * 60 * 60 * 1000

# parents is what tells us whether a changed file is even in our folder
CHANGE_FIELDS = (
    "newStartPageToken, nextPageToken, "
    "changes(fileId, removed, "
    "file(id, name, mimeType, modifiedTime, md5Checksum, webViewLink, trashed, parents))"
)

# Only one sync at a time: Drive can fire several notifications at once, and two
# concurrent runs would both advance the page token and lose changes.
_lock = threading.Lock()


def get_start_page_token(service):
    return service.changes().getStartPageToken().execute()["startPageToken"]


def ensure_page_token(service, state):
    if not state.get("page_token"):
        state["page_token"] = get_start_page_token(service)
        ingest.save_state(state)

    return state["page_token"]


# A file matters to us only while it's untrashed and still inside our folder.
# Moving a file out of the folder looks the same as deleting it, and should.
def is_in_folder(file):
    if not file or file.get("trashed"):
        return False

    return FOLDER_ID in (file.get("parents") or [])


def handle_change(service, index, change, state):
    file_id = change["fileId"]
    file = change.get("file")

    if change.get("removed") or not is_in_folder(file):
        if file_id in state["files"]:
            name = state["files"][file_id].get("name", file_id)
            store.delete_document(index, file_id)
            del state["files"][file_id]
            print(f"- {name}: removed from the index")
        return

    if ingest.unchanged(file, state):
        return

    print(f"- {file['name']}  ({file['mimeType']})")
    ingest.ingest_file(service, index, file, state)


def process_changes():
    # Blocks rather than skips: a notification that arrives mid-sync still needs
    # handling, and the page token means the second pass just finds nothing.
    with _lock:
        service = drive.get_drive_service()
        index = store.get_index()
        state = ingest.load_state()

        token = ensure_page_token(service, state)
        handled = 0

        while True:
            response = service.changes().list(
                pageToken=token,
                fields=CHANGE_FIELDS,
                pageSize=100,
            ).execute()

            for change in response.get("changes", []):
                handle_change(service, index, change, state)
                handled += 1

            # newStartPageToken appears on the last page — save it and stop
            if "newStartPageToken" in response:
                state["page_token"] = response["newStartPageToken"]
                break

            token = response["nextPageToken"]

        ingest.save_state(state)
        return handled


def watch(address):
    service = drive.get_drive_service()
    state = ingest.load_state()

    # Drive echoes this back on every notification, so we can tell a real
    # message from anyone else who finds the public URL.
    channel_token = secrets.token_urlsafe(24)
    channel_id = secrets.token_urlsafe(12)

    stop(quiet=True)   # drop any previous channel so it can't double-notify

    ensure_page_token(service, state)

    response = service.changes().watch(
        pageToken=state["page_token"],
        body={
            "id": channel_id,
            "type": "web_hook",
            "address": f"{address.rstrip('/')}/drive/webhook",
            "token": channel_token,
            "expiration": int(time.time() * 1000) + CHANNEL_LIFETIME_MS,
        },
    ).execute()

    state["channel"] = {
        "id": response["id"],
        "resource_id": response["resourceId"],
        "token": channel_token,
        "address": address.rstrip("/"),
        # Drive decides the real expiry, which may be sooner than we asked for
        "expiration": int(response.get("expiration", 0)),
    }
    ingest.save_state(state)

    expires = time.strftime("%Y-%m-%d %H:%M", time.localtime(state["channel"]["expiration"] / 1000))
    print(f"Watching for changes. Channel expires {expires}.")
    return state["channel"]


def stop(quiet=False):
    state = ingest.load_state()
    channel = state.get("channel")

    if not channel:
        if not quiet:
            print("No channel registered.")
        return

    service = drive.get_drive_service()

    try:
        service.channels().stop(
            body={"id": channel["id"], "resourceId": channel["resource_id"]}
        ).execute()
        print("Stopped watching for changes.")
    except Exception as error:
        # An expired channel can't be stopped, which is fine — it's already dead
        print(f"Could not stop the old channel (probably already expired): {error}")

    state["channel"] = None
    ingest.save_state(state)


# Channels expire, so re-register while there's still a day of life left.
def renew_if_expiring(within_ms=24 * 60 * 60 * 1000):
    state = ingest.load_state()
    channel = state.get("channel")

    if not channel:
        return False

    if int(time.time() * 1000) + within_ms < channel["expiration"]:
        return False

    print("Watch channel is near expiry — renewing.")
    watch(channel["address"])
    return True


def expected_token():
    return (ingest.load_state().get("channel") or {}).get("token")


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "poll"

    if command == "watch":
        if len(sys.argv) < 3:
            sys.exit("Usage: sync.py watch https://your-public-url")
        watch(sys.argv[2])

    elif command == "stop":
        stop()

    elif command == "poll":
        count = process_changes()
        print(f"Handled {count} change(s).")

    else:
        sys.exit(f"Unknown command: {command}")
