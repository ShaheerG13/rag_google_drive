# Step 5: the web app — one /ask endpoint, plus the HTML page that calls it
# Run it with: uvicorn app:app --reload

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import rag

app = FastAPI()


# Pydantic checks the incoming JSON for us: no "question" field → 422, not a crash
class Query(BaseModel):
    question: str


# Defined as a normal (non-async) function on purpose — answer() blocks while it
# waits on OpenAI and Pinecone, and FastAPI runs sync endpoints in a threadpool
# so one slow question doesn't freeze the whole server.
@app.post("/ask")
def ask(query: Query):
    return rag.answer(query.question)


# Must be mounted last: this catches every path not already claimed above, so /ask has to be registered first. html=True serves index.html at "/".
app.mount("/", StaticFiles(directory="static", html=True), name="static")
