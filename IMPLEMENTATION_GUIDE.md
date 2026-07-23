# RAG System — Google Drive Chat Integration

Implementation guide for a Retrieval-Augmented Generation (RAG) system that lets users
chat with the contents of a Google Drive folder. Knowledge base stays in sync
automatically. Storage and vector search run on **PostgreSQL + pgvector**.

---

## 1. What we're building (the mental model)

```
                     ┌─────────────────────────────────────────────┐
                     │                INGESTION                     │
  Google Drive  ───► │  list/download files → extract text →        │
  (one folder)       │  chunk → embed → store in Postgres (pgvector)│
                     └─────────────────────────────────────────────┘
                                        │
                                        ▼
                     ┌─────────────────────────────────────────────┐
                     │                 RETRIEVAL                    │
  User question ───► │  embed question → vector search in Postgres  │
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
| Vector store + DB  | PostgreSQL 16 + `pgvector`                | One database for metadata *and* vectors. Simple. |
| Drive access       | Google Drive API v3 (service account)     | Scoped to one folder; supports change tracking. |
| Text extraction    | `pypdf`, `python-docx`, Drive `export`    | Native export for Docs/Sheets/Slides. |
| Embeddings         | OpenAI `text-embedding-3-small` (1536-dim) | Cheap, good. Swap for local model later. |
| LLM                | Any chat model (OpenAI / Claude)          | Generates the grounded answer. |
| Backend            | Python + FastAPI                          | Small API + serves the web page. |
| Frontend           | Single HTML page (fetch → FastAPI)        | Ask a question, show answer + sources. |
| Sync (near-instant)| Drive **push notifications** (webhooks) + Changes API | Drive calls us the moment a file changes. |

> Learning tip: you can start fully local — swap OpenAI embeddings for
> `sentence-transformers/all-MiniLM-L6-v2` (384-dim) and a local Ollama model. The
> architecture below does not change, only the dimension number and the client call.

---

## 3. Database schema (Postgres + pgvector)

```sql
-- one-time setup
CREATE EXTENSION IF NOT EXISTS vector;

-- one row per Drive file we track (metadata + sync state)
CREATE TABLE documents (
    id            BIGSERIAL PRIMARY KEY,
    drive_file_id TEXT UNIQUE NOT NULL,     -- Google Drive file id
    name          TEXT NOT NULL,
    mime_type     TEXT,
    web_view_link TEXT,                      -- link back to the file (for citations)
    md5_checksum  TEXT,                      -- detect content changes
    modified_time TIMESTAMPTZ,              -- Drive's modifiedTime
    last_synced   TIMESTAMPTZ DEFAULT now()
);

-- one row per text chunk, with its embedding
CREATE TABLE chunks (
    id           BIGSERIAL PRIMARY KEY,
    document_id  BIGINT REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index  INT NOT NULL,
    content      TEXT NOT NULL,
    embedding    VECTOR(1536) NOT NULL       -- match your embedding model's dimension
);

-- approximate-nearest-neighbour index for fast search
CREATE INDEX ON chunks USING hnsw (embedding vector_cosine_ops);

-- store the Drive change cursor so we only fetch what changed
CREATE TABLE sync_state (
    id         INT PRIMARY KEY DEFAULT 1,
    page_token TEXT
);
```

Key idea: `ON DELETE CASCADE` means deleting a `documents` row auto-deletes its chunks —
that's how we handle **deleted files** cleanly.

---

## 4. Google Drive setup (scoped access)

1. In Google Cloud Console: create a project → enable the **Google Drive API**.
2. Create a **Service Account** and download its JSON key.
3. **Scope access to one folder only:** in Google Drive, share the target folder with the
   service account's email (`...@...iam.gserviceaccount.com`) as *Viewer*. The service
   account can now see **only** that folder — satisfying the "designated folder only"
   requirement.
4. Use the read-only scope: `https://www.googleapis.com/auth/drive.readonly`.

Grab the folder ID from its URL: `drive.google.com/drive/folders/<THIS_IS_THE_ID>`.

---

## 5. Ingestion pipeline

For each file in the folder:

1. **List** files under the folder:
   `files.list(q="'<folderId>' in parents and trashed=false")`.
2. **Extract text** by type:
   - Google Docs → `files.export(mimeType='text/plain')`
   - Google Sheets → `files.export(mimeType='text/csv')`
   - Google Slides → `files.export(mimeType='text/plain')`
   - PDF → download bytes, extract with `pypdf`
   - Plain text → download bytes directly
3. **Chunk** the text: ~500–800 tokens per chunk with ~10–15% overlap. A simple
   character-based splitter (e.g., 2000 chars, 200 overlap) is fine to start.
4. **Embed** each chunk (batch the API calls).
5. **Store**: upsert the `documents` row, delete its old `chunks`, insert new chunks +
   embeddings.

```python
# pseudocode — the core loop
for f in list_drive_files(folder_id):
    if unchanged(f):          # md5_checksum / modified_time matches DB → skip
        continue
    text   = extract_text(f)
    chunks = chunk_text(text, size=2000, overlap=200)
    vectors = embed(chunks)   # batch
    upsert_document_and_chunks(f, chunks, vectors)
```

---

## 6. Near-instant sync (push notifications)

We want the knowledge base to update **the moment** a file is added, changed, or deleted.
Drive's **push notifications** (`changes.watch`) do this: Drive sends an HTTP POST to *our*
webhook every time something changes in the folder. No polling.

**How it fits together:**
```
Drive change ──POST──► our /drive/webhook ──► run changes.list(pageToken) ──► reingest / delete
```
The webhook is just a *trigger* — it tells us "something changed," and we still call the
**Changes API** (`changes.list`) to fetch exactly *what* changed, using the stored page token.

**Setup — one time:** register a watch channel pointing at your public HTTPS endpoint.
```python
token = drive.changes.getStartPageToken()     # save to sync_state.page_token
drive.changes.watch(pageToken=token, body={
    "id": "my-channel-id",                     # any unique id
    "type": "web_hook",
    "address": "https://<your-domain>/drive/webhook",
    "expiration": <max ~1 week from now, in ms>
})
```

**Webhook handler** — runs on every change notification:
```python
@app.post("/drive/webhook")
def drive_webhook():
    token = load_page_token()
    while True:
        resp = drive.changes.list(pageToken=token,
                                  fields="changes,newStartPageToken,nextPageToken")
        for change in resp["changes"]:
            if change["removed"] or change["file"]["trashed"]:
                delete_document(change["fileId"])     # cascade removes chunks
            else:
                reingest(change["file"])              # new or updated file
        if "newStartPageToken" in resp:
            save_page_token(resp["newStartPageToken"]); break
        token = resp["nextPageToken"]
    return {"ok": True}
```

**Two things you must handle for reliability:**
- **Public HTTPS endpoint** — Drive must reach your server. Locally, tunnel with `ngrok`
  (`ngrok http 8000`) and use the https URL as `address`. In production, deploy behind HTTPS.
- **Channel expiry** — watch channels expire (max ~1 week). Schedule a small daily job
  (APScheduler) that calls `changes.watch` again to renew before expiry. This is the *only*
  timer in the system, and it's just for renewal — not for syncing.

**Safety net (optional):** also run `changes.list` once every few minutes so that if a
single webhook is ever missed, changes still get picked up shortly after.

---

## 7. Retrieval + answer generation

```python
def answer(question: str, k: int = 5):
    q_vec = embed([question])[0]

    # vector search: cosine distance, smaller = closer  (<=> is pgvector's cosine operator)
    rows = db.execute("""
        SELECT c.content, d.name, d.web_view_link,
               c.embedding <=> %s AS distance
        FROM chunks c
        JOIN documents d ON d.id = c.document_id
        ORDER BY c.embedding <=> %s
        LIMIT %s
    """, [q_vec, q_vec, k])

    context = "\n\n".join(f"[{i+1}] {r.content}" for i, r in enumerate(rows))
    prompt = f"""Answer the question using ONLY the context below.
If the answer isn't in the context, say you don't know. Cite sources as [1], [2], etc.

Context:
{context}

Question: {question}"""

    answer_text = llm.chat(prompt)
    sources = [{"name": r.name, "link": r.web_view_link} for r in rows]
    return {"answer": answer_text, "sources": sources}
```

This delivers the **grounded answer + citations** requirement. The "say you don't know"
instruction reduces hallucination when the folder doesn't contain the answer.

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
├── .env                    # DB url, Google key path, API keys (never commit)
├── requirements.txt
├── db.py                   # Postgres connection + schema setup
├── drive.py                # Drive auth, list, export/download, changes
├── ingest.py               # extract → chunk → embed → store
├── sync.py                 # changes.list logic + channel watch/renew
├── rag.py                  # retrieval + prompt + LLM  → answer()
├── app.py                  # FastAPI: /ask + serves static
└── static/
    └── index.html          # web UI
```

`requirements.txt` starter:
```
fastapi
uvicorn
psycopg[binary]
pgvector
google-api-python-client
google-auth
pypdf
python-docx
openai
apscheduler
python-dotenv
```

---

## 10. Build order (suggested milestones)

| # | Step | Tools / libraries used |
|---|------|------------------------|
| 1 | **DB up** — run Postgres, create the schema | Docker `pgvector/pgvector:pg16`, `psql`, `psycopg`, `pgvector` (Python) |
| 2 | **Drive read** — auth → list + print folder files | `google-auth`, `google-api-python-client` (Drive API v3) |
| 3 | **Ingest once** — extract → chunk → embed → store | Drive `files.export`/`download`, `pypdf`, `python-docx`, `openai` (embeddings), `psycopg` |
| 4 | **Ask via script** — implement `answer()`, test retrieval | `openai` (embeddings + chat LLM), `pgvector` cosine search (`<=>`) |
| 5 | **Web UI** — wire `/ask` + HTML page | `fastapi`, `uvicorn`, plain HTML/JS `fetch` |
| 6 | **Near-instant sync** — webhook + change fetch + renewal | Drive `changes.watch` / `changes.list`, `ngrok` (public HTTPS), `apscheduler` (daily channel renewal) |

Ship milestones 1–5 first (a working RAG chat), then add push-based sync in step 6.

---

## 11. Things to watch (learning notes)

- **Embedding dimension must match** the `VECTOR(n)` column. Change the model → change the column.
- **Cosine distance operator** in pgvector is `<=>`. Use it consistently with `vector_cosine_ops` on the index.
- **Chunking quality drives answer quality** more than the LLM choice. Tune size/overlap early.
- **Idempotent ingestion:** always delete a document's old chunks before inserting new ones, or you'll get duplicates on re-sync.
- **Rate limits:** batch embedding calls; add simple retry/backoff.
- **Secrets:** keep the service-account JSON and API keys in `.env` / outside git.
```
