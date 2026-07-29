# Step 4: ask a question → find the closest chunks in Pinecone → have the LLM answer using only those
#
# Run it with: 'python rag.py "your question here"' or with no argument for an interactive prompt

import os
import sys

from dotenv import load_dotenv
from openai import OpenAI

import store
from ingest import embed

load_dotenv()

CHAT_MODEL = "gpt-4o-mini"

TOP_K = 5 # how many chunks to feed the model
MIN_SCORE = 0.2

openai = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

SYSTEM_PROMPT = """You answer questions about the user's documents.
Use ONLY the numbered context provided. If the answer isn't in the context, say
you don't know — never fill gaps from your own knowledge.
Cite the sources you used inline as [1], [2], etc."""


def search(index, question, k=TOP_K):
    # The question goes through the same embedding model as the chunks did, so they land in the same vector space and can be compared.
    question_vector = embed([question])[0]

    results = index.query(vector=question_vector, top_k=k, include_metadata=True)

    # Pinecone returns similarity (higher = closer), the opposite of a distance
    return [m for m in results["matches"] if m["score"] >= MIN_SCORE]


def build_context(matches):
    return "\n\n".join(
        f"[{i + 1}] (from {m['metadata']['name']})\n{m['metadata']['text']}"
        for i, m in enumerate(matches)
    )


def answer(question, k=TOP_K):
    index = store.get_index()
    matches = search(index, question, k)

    if not matches:
        return {"answer": "I couldn't find anything relevant in your Drive folder.", "sources": []}

    prompt = f"Context:\n{build_context(matches)}\n\nQuestion: {question}"

    response = openai.chat.completions.create(
        model=CHAT_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    )

    # One entry per citation number, in the same order the prompt numbered them.
    sources = [
        {
            "name": m["metadata"]["name"],
            "link": m["metadata"]["web_view_link"],
            "score": round(m["score"], 3),
        }
        for m in matches
    ]

    return {"answer": response.choices[0].message.content, "sources": sources}


def ask_and_print(question):
    result = answer(question)

    print(f"\n{result['answer']}\n")

    if result["sources"]:
        print("Sources:")
        for i, s in enumerate(result["sources"], start=1):
            print(f"  [{i}] {s['name']}  (score {s['score']})  {s['link']}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        ask_and_print(" ".join(sys.argv[1:]))
    else:
        # No question given — keep asking until Ctrl+C or an empty line.
        while True:
            question = input("\nAsk a question (blank to quit): ").strip()
            if not question:
                break
            ask_and_print(question)
