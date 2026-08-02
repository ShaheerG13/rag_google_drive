# Step 5: the web app — one /ask endpoint, plus the HTML page that calls it
# Step 6 adds /drive/webhook, which Drive calls whenever the folder changes
# Run it with: uvicorn app:app --reload

from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import BackgroundTasks, FastAPI, Header, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import rag
import sync

scheduler = BackgroundScheduler()


@asynccontextmanager
async def lifespan(app):
    # Channels expire after about a week, so check daily and re-register early
    scheduler.add_job(sync.renew_if_expiring, "interval", hours=12)

    # Safety net: if a single notification is ever missed (server down, tunnel
    # restarted), this picks the change up within a few minutes anyway
    scheduler.add_job(sync.process_changes, "interval", minutes=5)

    scheduler.start()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(lifespan=lifespan)


# Pydantic checks the incoming JSON for us: no "question" field → 422, not a crash
class Query(BaseModel):
    question: str


# Defined as a normal (non-async) function on purpose — answer() blocks while it
# waits on OpenAI and Pinecone, and FastAPI runs sync endpoints in a threadpool
# so one slow question doesn't freeze the whole server.
@app.post("/ask")
def ask(query: Query):
    return rag.answer(query.question)


# Drive calls this on every change. It never says what changed, only that
# something did — process_changes() asks Drive for the details.
@app.post("/drive/webhook")
def drive_webhook(
    background: BackgroundTasks,
    x_goog_channel_token: str = Header(default=""),
    x_goog_resource_state: str = Header(default=""),
):
    # The URL is public, so check the secret Drive echoes back to us
    if x_goog_channel_token != sync.expected_token():
        return Response(status_code=403)

    # Drive sends one "sync" handshake when the channel is created — nothing changed yet
    if x_goog_resource_state == "sync":
        return {"ok": True}

    # Answer immediately and do the work after: Drive retries if we're slow,
    # which would pile up duplicate syncs.
    background.add_task(sync.process_changes)
    return {"ok": True}


# Must be mounted last: this catches every path not already claimed above, so /ask has to be registered first. html=True serves index.html at "/".
app.mount("/", StaticFiles(directory="static", html=True), name="static")
