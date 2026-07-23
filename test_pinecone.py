import os
from dotenv import load_dotenv
from pinecone import Pinecone, ServerlessSpec

load_dotenv()

api_key = os.environ["PINECONE_API_KEY"]

pc = Pinecone(api_key=api_key)

# Settings for our index.
# name: what we'll call our vector "table"
# dimension: MUST match the embedding model. OpenAI text-embedding-3-small = 1536
# metric: "cosine" measures how similar two vectors are (standard for text search)
INDEX_NAME = "drive-rag"
DIMENSION = 1536

# Create the index only if it doesn't already exist (so re-running is safe)
existing = [i["name"] for i in pc.list_indexes()]
if INDEX_NAME not in existing:
    print(f"Creating index '{INDEX_NAME}'...")
    pc.create_index(
        name=INDEX_NAME,
        dimension=DIMENSION,
        metric="cosine",
        spec=ServerlessSpec(cloud="aws", region="us-east-1"),
    )
    print("Index created.")
else:
    print(f"Index '{INDEX_NAME}' already exists.")

# Fetch and print the index stats to prove the connection works end-to-end.
index = pc.Index(INDEX_NAME)
print("Connection OK. Index stats:")
print(index.describe_index_stats())
