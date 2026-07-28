# Step 3: talking to Pinecone — upsert chunks, delete a file's chunks, count what's stored

import os
from dotenv import load_dotenv
from pinecone import Pinecone

load_dotenv()

INDEX_NAME = "drive-rag"


def get_index():
    pc = Pinecone(api_key=os.environ["PINECONE_API_KEY"])
    return pc.Index(INDEX_NAME)


# Every chunk's id is "{drive_file_id}#{chunk_index}"
# The shared prefix is what lets us find all of one file's chunks again
# Serverless Pinecone can't deleteby metadata filter, only by id
def chunk_id(drive_file_id, chunk_index):
    return f"{drive_file_id}#{chunk_index}"


def delete_document(index, drive_file_id):
    deleted = 0

    for page in index.list(prefix=f"{drive_file_id}#"):
        ids = [v.id for v in page.vectors]
        if ids:
            index.delete(ids=ids)
            deleted += len(ids)

    return deleted


def upsert_chunks(index, file, chunks, vectors):
    records = [
        {
            "id": chunk_id(file["id"], i),
            "values": vector,
            "metadata": {
                "drive_file_id": file["id"],
                "name": file["name"],
                "web_view_link": file.get("webViewLink", ""),
                "chunk_index": i,
                # The text rides along with the vector — Pinecone gives back metadata on a query, and the LLM prompt needs the actual words
                "text": chunk,
            },
        }
        for i, (chunk, vector) in enumerate(zip(chunks, vectors))
    ]

    # Upsert in batches so we never send a huge request
    for start in range(0, len(records), 100):
        index.upsert(vectors=records[start:start + 100])

    return len(records)
