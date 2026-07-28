# Step 3: pull every file in the folder → extract text → chunk → embed → store in Pinecone
# Safe to re-run: files that haven't changed since last time are skipped, and a file that has changed gets its old chunks deleted before the new ones go in

import io
import json
import os

from dotenv import load_dotenv
from openai import OpenAI

import drive
import store

load_dotenv()

STATE_FILE = "sync_state.json"

EMBED_MODEL = "text-embedding-3-small" # 1536 dimensions

CHUNK_SIZE = 2000
CHUNK_OVERLAP = 200

openai = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

# Google-native types have no bytes to download — they must be exported to a text format
GOOGLE_EXPORTS = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"



# Remembers what we already ingested, so re-runs only do the new work
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"page_token": None, "files": {}}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# A file counts as unchanged if its checksum matches
# Google-native files don't have a checksum, so for those we compare the last-modified timestamp instead
def unchanged(file, state):
    seen = state["files"].get(file["id"])
    if not seen:
        return False

    if file.get("md5Checksum"):
        return seen.get("md5") == file["md5Checksum"]

    return seen.get("modified_time") == file["modifiedTime"]



def extract_text(service, file):
    mime = file["mimeType"]

    if mime in GOOGLE_EXPORTS:
        data = drive.export_file(service, file["id"], GOOGLE_EXPORTS[mime])
        return data.decode("utf-8", errors="ignore")

    if mime == "application/pdf":
        from pypdf import PdfReader

        data = drive.download_file(service, file["id"])
        reader = PdfReader(io.BytesIO(data))
        # A page with no extractable text (e.g. a scan) returns None, so default to ""
        return "\n".join(page.extract_text() or "" for page in reader.pages)

    if mime == DOCX:
        import docx

        data = drive.download_file(service, file["id"])
        document = docx.Document(io.BytesIO(data))
        return "\n".join(p.text for p in document.paragraphs)

    if mime.startswith("text/"):
        data = drive.download_file(service, file["id"])
        return data.decode("utf-8", errors="ignore")

    # Images, video, anything else we can't read — caller skips these
    return None


def chunk_text(text, size=CHUNK_SIZE, overlap=CHUNK_OVERLAP):
    text = text.strip()
    if not text:
        return []

    chunks = []
    start = 0

    while start < len(text):
        chunk = text[start:start + size].strip()
        if chunk:
            chunks.append(chunk)
        # Step forward by less than a full chunk, so consecutive chunks overlap
        start += size - overlap

    return chunks


def embed(texts):
    vectors = []

    # The API takes many inputs per call — batching is far faster than one at a time
    for start in range(0, len(texts), 100):
        batch = texts[start:start + 100]
        response = openai.embeddings.create(model=EMBED_MODEL, input=batch)
        vectors.extend(item.embedding for item in response.data)

    return vectors


def ingest_file(service, index, file, state):
    text = extract_text(service, file)

    if text is None:
        print(f"  skipped (unsupported type: {file['mimeType']})")
        return 0

    chunks = chunk_text(text)
    if not chunks:
        print("  skipped (no text found)")
        return 0

    vectors = embed(chunks)

    # Delete first: if the file shrank, stale chunks would otherwise linger
    store.delete_document(index, file["id"])
    store.upsert_chunks(index, file, chunks, vectors)

    state["files"][file["id"]] = {
        "md5": file.get("md5Checksum"),
        "modified_time": file["modifiedTime"],
        "name": file["name"],
    }

    print(f"  {len(chunks)} chunk(s) stored")
    return len(chunks)


def main():
    service = drive.get_drive_service()
    index = store.get_index()
    state = load_state()

    files = drive.list_files(service)
    print(f"Found {len(files)} file(s) in the folder.\n")

    total_chunks = 0
    skipped = 0

    for file in files:
        if unchanged(file, state):
            skipped += 1
            continue

        print(f"- {file['name']}  ({file['mimeType']})")
        total_chunks += ingest_file(service, index, file, state)
        save_state(state)

    # Files we ingested before that are no longer in the folder: drop their chunks
    present = {f["id"] for f in files}
    for file_id in [i for i in state["files"] if i not in present]:
        name = state["files"][file_id].get("name", file_id)
        removed = store.delete_document(index, file_id)
        del state["files"][file_id]
        print(f"- {name}: removed ({removed} chunk(s) deleted)")

    save_state(state)

    print(f"\nDone. {total_chunks} chunk(s) ingested, {skipped} file(s) unchanged.")
    print(index.describe_index_stats())


if __name__ == "__main__":
    main()
