"""
rag/pipeline.py

Fetches configured GitHub docs, chunks them, and returns raw text context for
Codex prompt injection. No embeddings or vector database are used.
"""

from __future__ import annotations

import hashlib
import logging
import requests
from typing import List

from config.settings import GITHUB_REPO, GITHUB_TOKEN, DOCS_PATHS

logger = logging.getLogger(__name__)

CHUNK_SIZE = 500
CHUNK_OVERLAP = 50


def fetch_github_docs() -> List[dict]:
    headers = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"token {GITHUB_TOKEN}"

    docs = []
    for path in DOCS_PATHS:
        path = path.strip()
        if not path:
            continue

        url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code != 200:
            logger.warning(f"GitHub fetch failed for {path}: {resp.status_code}")
            continue

        data = resp.json()
        if isinstance(data, dict) and data.get("type") == "file":
            text = requests.get(data["download_url"], timeout=15).text
            docs.append({"source": path, "text": text})
        elif isinstance(data, list):
            for item in data:
                if item.get("name", "").endswith(".md"):
                    text = requests.get(item["download_url"], timeout=15).text
                    docs.append({"source": item["path"], "text": text})

    return docs


def chunk_text(text: str, source: str) -> List[dict]:
    words = text.split()
    step = CHUNK_SIZE - CHUNK_OVERLAP
    chunks = []
    for i in range(0, len(words), step):
        chunk = " ".join(words[i:i + CHUNK_SIZE])
        chunk_id = hashlib.md5(f"{source}_{i}".encode()).hexdigest()
        chunks.append({"id": chunk_id, "source": source, "text": chunk})
    return chunks


def all_docs_context() -> str:
    """Return all configured GitHub doc chunks for direct prompt injection."""
    if not GITHUB_REPO:
        return ""

    docs = fetch_github_docs()
    chunks = []
    for doc in docs:
        chunks.extend(chunk_text(doc["text"], doc["source"]))

    if not chunks:
        return ""

    return "\n\n---\n\n".join(
        f"[{chunk['source']}]\n{chunk['text']}"
        for chunk in chunks
    )
