# RAG System — Google Drive Chat Integration

Implementation guide for a Retrieval-Augmented Generation (RAG) system that lets users
chat with the contents of a Google Drive folder. Knowledge base stays in sync
automatically. Vector storage and search run on **Pinecone** (serverless).

---

## 1. What we're building (the mental model)

```
                     ┌─────────────────────────────────────────────┐
                     │                INGESTION                     │
  Google Drive  ───► │  list/download files → extract text →        │
  (one folder)       │  chunk → embed → upsert into Pinecone        │
                     └─────────────────────────────────────────────┘
                                        │
                                        ▼
                     ┌─────────────────────────────────────────────┐
                     │                 RETRIEVAL                    │
  User question ───► │  embed question → vector search in Pinecone  │
   (web UI)          │  → top-k chunks → build prompt → LLM answer  │ ───► Answer + citations
                     └─────────────────────────────────────────────┘
```

**RAG in one line:** instead of asking the LLM to answer from memory, we first *retrieve*
the most relevant pieces of your documents, then ask the LLM to answer *using only those pieces*.
This grounds answers in your data and lets us cite sources.

---

## 2. Tech stack

| Concern            | Choice                                    | Why |
|--------------------|-------------------------------------------|-----|
| Vector store       | Pinecone serverless (`drive-rag` index)   | Managed vector DB — no server to run, free tier is plenty here. |
| Chunk text + metadata | Pinecone record metadata                | Pinecone stores the chunk text alongside its vector, so no second database. |
| Sync state         | Local `sync_state.json`                   | Holds the Drive change page token + per-file checksums. |
| Drive access       | Google Drive API v3, **OAuth desktop client** | You log in as yourself once; read-only scope. |
| Text extraction    | `pypdf`, `python-docx`, Drive `export`    | Native export for Docs/Sheets/Slides. |
| Embeddings         | OpenAI `text-embedding-3-small` (1536-dim) | Cheap, good. Must match the index dimension. |
| LLM                | Any chat model (OpenAI / Claude)          | Generates the grounded answer. |
| Backend            | Python + FastAPI                          | Small API + serves the web page. |
| Frontend           | Single HTML page (fetch → FastAPI)        | Ask a question, show answer + sources. |
| Sync (near-instant)| Drive **push notifications** (webhooks) + Changes API | Drive calls us the moment a file changes. |

> Learning tip: you can swap OpenAI embeddings for
> `sentence-transformers/all-MiniLM-L6-v2` (384-dim) and a local Ollama model. The
> architecture below does not change — but the Pinecone index dimension must be recreated
> to match, since an index's dimension is fixed at creation.

---

## 3. Pinecone index + record design

There are no tables to create — one index, created once (see `test_pinecone.py`):

```python
pc.create_index(
    name="drive-rag",
    dimension=1536,                 # MUST match the embedding model
    metric="cosine",
    spec=ServerlessSpec(cloud="aws", region="us-east-1"),
)
```

Each **chunk** becomes one Pinecone record:

```python
{
    "id": f"{drive_file_id}#{chunk_index}",   # deterministic — see below
    "values": embedding,                       # 1536 floats
    "metadata": {
        "drive_file_id": drive_file_id,
        "name":          "Q3 Report.pdf",
        "web_view_link": "https://drive.google.com/file/d/.../view",
        "chunk_index":   0,
        "text":          "the actual chunk text ...",   # needed for the prompt
    },
}
```

**Why the `{file_id}#{chunk_index}` ID scheme matters.** Pinecone **serverless indexes do
not support delete-by-metadata-filter**. The supported way to remove every chunk of one
file is to delete **by ID prefix**: because the IDs share the prefix `{file_id}#`, we can
list and delete them.

```python
def delete_document(index, drive_file_id):
    deleted = 0

    for page in index.list(prefix=f"{drive_file_id}#"):   # paginates for you
        ids = [v.id for v in page.vectors]
        if ids:                                            # skip the empty first-ingest case
            index.delete(ids=ids)
            deleted += len(ids)

    return deleted
```

Two traps here, both easy to hit:

- **`index.list()` yields pages, not IDs.** In the current SDK (9.x) each item is a
  `ListResponse` whose IDs live at `page.vectors`, each with a `.id`. Older SDKs yielded
  plain lists of ID strings, so plenty of tutorials still show `for ids in index.list(...)`
  — that form silently breaks here.
- **`index.delete()` accepts a `filter=` argument that serverless rejects at runtime.** The
  signature suggests delete-by-metadata works; it doesn't. Use the prefix approach above.

This replaces what `ON DELETE CASCADE` would do in a relational DB, and it's how we handle
both **deleted files** and **re-ingesting a changed file** (delete old chunks, insert new).

**Sync state** lives in a local `sync_state.json` (gitignored) rather than a database:

```json
{
  "page_token": "128473",
  "files": {
    "1AbC...xyz": { "md5": "9f2b...", "modified_time": "2026-07-27T12:00:00Z" }
  }
}
```

`page_token` is the Drive changes cursor; `files` lets us skip files that haven't changed.

---

## 4. Google Drive setup (OAuth desktop client)

We authenticate as **you**, via a one-time browser login — not a service account. This is
simpler for a personal Drive folder and is what `drive.py` implements.

1. In [Google Cloud Console](https://console.cloud.google.com): create/select a project.
2. **APIs & Services → Library** → enable the **Google Drive API**.
3. **APIs & Services → OAuth consent screen**:
   - Set **Audience** to **External**. (If it is **Internal**, only accounts inside the
     project's Workspace organization can sign in — a personal Gmail account gets
     *"Access blocked: … can only be used within its organization"*.)
   - Publishing status can stay **Testing**.
   - Add your own Google account under **Test users**, or login will be refused.
4. **APIs & Services → Credentials → Create Credentials → OAuth client ID** →
   application type **Desktop app** → download the JSON.
5. Save that file as **`credentials.json`** in the project root — that exact filename is
   what `drive.py` reads. It is gitignored.
6. Put the folder ID in `.env` as `DRIVE_FOLDER_ID`. Grab it from the folder's URL:
   `drive.google.com/drive/folders/<THIS_IS_THE_ID>`.

First run opens a browser; approve access and a **`token.json`** is written so later runs
don't prompt. Expect an "unverified app" warning while the app is in Testing —
**Advanced → Go to \<app\> (unsafe)** is the normal path for your own client.

**Scope:** `https://www.googleapis.com/auth/drive.readonly` — we can read but never modify.

> Honest caveat: that scope grants read access to *all* of your Drive. Restricting the
> system to one folder is enforced by **our query** (`'<folderId>' in parents`), not by the
> permission itself. If you need permission-level scoping to a single folder, use a service
> account instead and share only that folder with its `...iam.gserviceaccount.com` address.

---

## 5. Ingestion pipeline

For each file in the folder:

1. **List** files under the folder:
   `files.list(q="'<folderId>' in parents and trashed=false")`, requesting
   `fields="files(id, name, mimeType, modifiedTime, md5Checksum, webViewLink)"`.
2. **Extract text** by type:
   - Google Docs → `files.export(mimeType='text/plain')`
   - Google Sheets → `files.export(mimeType='text/csv')`
   - Google Slides → `files.export(mimeType='text/plain')`
   - PDF → download bytes, extract with `pypdf`
   - Plain text → download bytes directly
3. **Chunk** the text: ~500–800 tokens per chunk with ~10–15% overlap. A simple
   character-based splitter (e.g., 2000 chars, 200 overlap) is fine to start.
4. **Embed** each chunk (batch the API calls).
5. **Store**: delete the file's existing chunks by ID prefix, then upsert the new records.

```python
# pseudocode — the core loop
for f in list_drive_files(folder_id):
    if unchanged(f):              # md5Checksum / modifiedTime matches sync_state → skip
        continue
    text    = extract_text(f)
    chunks  = chunk_text(text, size=2000, overlap=200)
    vectors = embed(chunks)       # batch

    delete_document(index, f["id"])          # drop stale chunks first
    index.upsert(vectors=[
        {
            "id": f"{f['id']}#{i}",
            "values": vec,
            "metadata": {
                "drive_file_id": f["id"],
                "name": f["name"],
                "web_view_link": f.get("webViewLink", ""),
                "chunk_index": i,
                "text": chunk,
            },
        }
        for i, (chunk, vec) in enumerate(zip(chunks, vectors))
    ])
    record_synced(f)              # write md5 / modifiedTime into sync_state.json
```

Note: Google-native files (Docs/Sheets/Slides) have **no `md5Checksum`** — fall back to
`modifiedTime` for the unchanged check on those.

---

## 6. Near-instant sync (push notifications)

We want the knowledge base to update **the moment** a file is added, changed, or deleted.
Drive's **push notifications** (`changes.watch`) do this: Drive sends an HTTP POST to *our*
webhook every time something changes. No polling.

**How it fits together:**
```
Drive change ──POST──► our /drive/webhook ──► run changes.list(pageToken) ──► reingest / delete
```
The webhook is just a *trigger* — it tells us "something changed," and we still call the
**Changes API** (`changes.list`) to fetch exactly *what* changed, using the stored page token.

**Setup — one time:** register a watch channel pointing at your public HTTPS endpoint.
```python
token = drive.changes().getStartPageToken().execute()["startPageToken"]
save_page_token(token)                         # → sync_state.json
drive.changes().watch(pageToken=token, body={
    "id": "my-channel-id",                     # any unique id
    "type": "web_hook",
    "address": "https://<your-domain>/drive/webhook",
    "expiration": <max ~1 week from now, in ms>,
}).execute()
```

**Webhook handler** — runs on every change notification:
```python
@app.post("/drive/webhook")
def drive_webhook():
    token = load_page_token()
    while True:
        resp = drive.changes().list(
            pageToken=token,
            fields="changes(fileId,removed,file(id,name,mimeType,trashed,md5Checksum,modifiedTime,webViewLink,parents)),newStartPageToken,nextPageToken",
        ).execute()
        for change in resp.get("changes", []):
            if change.get("removed") or change.get("file", {}).get("trashed"):
                delete_document(index, change["fileId"])   # by ID prefix
            elif FOLDER_ID in change.get("file", {}).get("parents", []):
                reingest(change["file"])                   # new or updated file
        if "newStartPageToken" in resp:
            save_page_token(resp["newStartPageToken"]); break
        token = resp["nextPageToken"]
    return {"ok": True}
```

**Three things you must handle for reliability:**
- **Public HTTPS endpoint** — Drive must reach your server. Locally, tunnel with `ngrok`
  (`ngrok http 8000`) and use the https URL as `address`. In production, deploy behind HTTPS.
- **Channel expiry** — watch channels expire (max ~1 week). Schedule a small daily job
  (APScheduler) that calls `changes.watch` again to renew before expiry. This is the *only*
  timer in the system, and it's just for renewal — not for syncing.
- **`changes.list` is Drive-wide** — it reports changes to *everything* you can see, not
  just our folder. Filter on `parents` (as above) so we ingest only the target folder.

**Safety net (optional):** also run `changes.list` once every few minutes so that if a
single webhook is ever missed, changes still get picked up shortly after.

---

## 7. Retrieval + answer generation

```python
def answer(question: str, k: int = 5):
    q_vec = embed([question])[0]

    res = index.query(vector=q_vec, top_k=k, include_metadata=True)
    matches = res["matches"]        # already sorted best-first; score = cosine similarity

    context = "\n\n".join(
        f"[{i+1}] {m['metadata']['text']}" for i, m in enumerate(matches)
    )
    prompt = f"""Answer the question using ONLY the context below.
If the answer isn't in the context, say you don't know. Cite sources as [1], [2], etc.

Context:
{context}

Question: {question}"""

    answer_text = llm.chat(prompt)
    sources = [
        {"name": m["metadata"]["name"], "link": m["metadata"]["web_view_link"]}
        for m in matches
    ]
    return {"answer": answer_text, "sources": sources}
```

This delivers the **grounded answer + citations** requirement. The "say you don't know"
instruction reduces hallucination when the folder doesn't contain the answer.

Note that Pinecone returns a **similarity score** (cosine: higher = closer), the opposite
direction from a distance metric. If you filter weak matches, drop results *below* a
threshold, e.g. `[m for m in matches if m["score"] > 0.3]`.

---

## 8. Web interface

**Backend** — FastAPI with two things: an `/ask` endpoint and serving the HTML page.

```python
# app.py
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI()

class Query(BaseModel):
    question: str

@app.post("/ask")
def ask(q: Query):
    return answer(q.question)     # from section 7

app.mount("/", StaticFiles(directory="static", html=True), name="static")
```

**Frontend** — one `static/index.html`: a text box, a button, and a results area.

```html
<!doctype html>
<html>
<body style="font-family:sans-serif;max-width:700px;margin:40px auto">
  <h2>Chat with your Drive</h2>
  <input id="q" style="width:80%" placeholder="Ask a question..." />
  <button onclick="ask()">Ask</button>
  <div id="answer" style="margin-top:20px;white-space:pre-wrap"></div>
  <ul id="sources"></ul>
  <script>
    async function ask() {
      const question = document.getElementById('q').value;
      document.getElementById('answer').textContent = 'Thinking...';
      const res = await fetch('/ask', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ question })
      });
      const data = await res.json();
      document.getElementById('answer').textContent = data.answer;
      document.getElementById('sources').innerHTML =
        data.sources.map(s => `<li><a href="${s.link}" target="_blank">${s.name}</a></li>`).join('');
    }
  </script>
</body>
</html>
```

Run it: `uvicorn app:app --reload` → open `http://localhost:8000`.

---

## 9. Project structure

```
rag_google_drive/
├── IMPLEMENTATION_GUIDE.md
├── .env                    # Pinecone + OpenAI keys, Drive folder id (never commit)
├── credentials.json        # OAuth client secret from Google Cloud (never commit)
├── token.json              # saved login, created on first run (never commit)
├── sync_state.json         # page token + per-file checksums (never commit)
├── requirements.txt
├── test_pinecone.py        # step 1: create index + prove the connection works
├── store.py                # Pinecone client, upsert / delete-by-prefix / query
├── drive.py                # Drive auth, list, export/download, changes
├── ingest.py               # extract → chunk → embed → store
├── sync.py                 # changes.list logic + channel watch/renew
├── rag.py                  # retrieval + prompt + LLM  → answer()
├── app.py                  # FastAPI: /ask + serves static
└── static/
    └── index.html          # web UI
```

`requirements.txt` (grows per step):
```
# Step 1 — vector store
pinecone
python-dotenv
typing_extensions

# Step 2 — Google Drive via OAuth
google-api-python-client
google-auth
google-auth-oauthlib

# Step 3 — extraction + embeddings
pypdf
python-docx
openai

# Steps 5–6 — web app + sync
fastapi
uvicorn
apscheduler
```

---

## 10. Build order (suggested milestones)

| # | Step | Tools / libraries used |
|---|------|------------------------|
| 1 | **Index up** — create the Pinecone index, confirm connection | `pinecone` (`ServerlessSpec`, `describe_index_stats`) |
| 2 | **Drive read** — auth → list + print folder files | `google-auth-oauthlib` (OAuth flow), `google-api-python-client` (Drive API v3) |
| 3 | **Ingest once** — extract → chunk → embed → upsert | Drive `files.export`/`get_media`, `pypdf`, `python-docx`, `openai` (embeddings), `pinecone` |
| 4 | **Ask via script** — implement `answer()`, test retrieval | `openai` (embeddings + chat LLM), Pinecone `index.query` |
| 5 | **Web UI** — wire `/ask` + HTML page | `fastapi`, `uvicorn`, plain HTML/JS `fetch` |
| 6 | **Near-instant sync** — webhook + change fetch + renewal | Drive `changes.watch` / `changes.list`, `ngrok` (public HTTPS), `apscheduler` (daily channel renewal) |

Ship milestones 1–5 first (a working RAG chat), then add push-based sync in step 6.

---

## 11. Things to watch (learning notes)

- **Embedding dimension is fixed at index creation.** `text-embedding-3-small` = 1536. Switch
  models → delete and recreate the index with the new dimension.
- **Serverless Pinecone can't delete by metadata filter.** Delete by ID prefix instead —
  which is the whole reason chunk IDs are `{drive_file_id}#{chunk_index}`.
- **Upserts are eventually consistent.** A freshly upserted chunk may not appear in query
  results for a second or two. Don't write a test that queries immediately after upserting
  without a short wait.
- **Store the chunk text in metadata** — Pinecone returns vectors, not text, and the prompt
  needs the actual words. Metadata has a ~40 KB per-record limit, so keep chunks well under it.
- **Chunking quality drives answer quality** more than the LLM choice. Tune size/overlap early.
- **Idempotent ingestion:** always delete a document's old chunks before inserting new ones,
  or a shrinking file leaves orphaned chunks behind.
- **Rate limits:** batch embedding calls; add simple retry/backoff.
- **Secrets:** keep `credentials.json`, `token.json`, and API keys in `.env` / outside git.
```
