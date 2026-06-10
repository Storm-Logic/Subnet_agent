"""
knowledge_store.py

Persists reference links and manual Q&A pairs as JSON files.
The admin web UI reads/writes via the FastAPI server.
The bot reads from these on every query.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent
LINKS_FILE = DATA_DIR / "reference_links.json"
QA_FILE    = DATA_DIR / "manual_qa.json"
ANNOUNCEMENTS_FILE = DATA_DIR / "announcements.json"
INFO_FILE = DATA_DIR / "information.json"

_lock = threading.RLock()


def _load(path: Path) -> list:
    if path.exists():
        try:
            with open(path) as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Load failed {path}: {e}")
    return []

def _save(path: Path, data: list):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

def _id() -> str:
    return uuid.uuid4().hex[:8]


# ── Reference links ───────────────────────────────────────────────────────────

def get_links() -> List[dict]:
    with _lock:
        return _load(LINKS_FILE)

def add_link(label: str, url: str, description: str = "") -> dict:
    with _lock:
        links = _load(LINKS_FILE)
        entry = {"id": _id(), "label": label, "url": url,
                 "description": description, "added": datetime.utcnow().isoformat()}
        links.append(entry)
        _save(LINKS_FILE, links)
        return entry

def update_link(link_id: str, label=None, url=None, description=None) -> Optional[dict]:
    with _lock:
        links = _load(LINKS_FILE)
        for l in links:
            if l["id"] == link_id:
                if label is not None: l["label"] = label
                if url is not None: l["url"] = url
                if description is not None: l["description"] = description
                _save(LINKS_FILE, links)
                return l
        return None

def remove_link(link_id: str) -> bool:
    with _lock:
        links = _load(LINKS_FILE)
        new = [l for l in links if l["id"] != link_id]
        if len(new) == len(links): return False
        _save(LINKS_FILE, new)
        return True


# ── Manual Q&A ────────────────────────────────────────────────────────────────

def get_qa_pairs() -> List[dict]:
    with _lock:
        return _load(QA_FILE)

def add_qa(question: str, answer: str, tags: List[str] = None) -> dict:
    with _lock:
        pairs = _load(QA_FILE)
        entry = {"id": _id(), "question": question, "answer": answer,
                 "tags": tags or [], "added": datetime.utcnow().isoformat()}
        pairs.append(entry)
        _save(QA_FILE, pairs)
        return entry

def update_qa(qa_id: str, question=None, answer=None, tags=None) -> Optional[dict]:
    with _lock:
        pairs = _load(QA_FILE)
        for p in pairs:
            if p["id"] == qa_id:
                if question is not None: p["question"] = question
                if answer is not None:   p["answer"] = answer
                if tags is not None:     p["tags"] = tags
                _save(QA_FILE, pairs)
                return p
        return None

def remove_qa(qa_id: str) -> bool:
    with _lock:
        pairs = _load(QA_FILE)
        new = [p for p in pairs if p["id"] != qa_id]
        if len(new) == len(pairs): return False
        _save(QA_FILE, new)
        return True


# ── Information ───────────────────────────────────────────────────────────────

def get_information() -> List[dict]:
    with _lock:
        return _load(INFO_FILE)

def add_information(body: str) -> dict:
    with _lock:
        items = _load(INFO_FILE)
        entry = {
            "id": _id(),
            "body": body,
            "added": datetime.utcnow().isoformat(),
        }
        items.append(entry)
        _save(INFO_FILE, items)
        return entry

def update_information(info_id: str, body=None) -> Optional[dict]:
    with _lock:
        items = _load(INFO_FILE)
        for item in items:
            if item["id"] == info_id:
                if body is not None:  item["body"] = body
                _save(INFO_FILE, items)
                return item
        return None

def remove_information(info_id: str) -> bool:
    with _lock:
        items = _load(INFO_FILE)
        new = [item for item in items if item["id"] != info_id]
        if len(new) == len(items): return False
        _save(INFO_FILE, new)
        return True


# ── Announcements ─────────────────────────────────────────────────────────────

def get_announcements() -> List[dict]:
    with _lock:
        return _load(ANNOUNCEMENTS_FILE)

def add_announcement(title: str, body: str, active: bool = True) -> dict:
    with _lock:
        announcements = _load(ANNOUNCEMENTS_FILE)
        entry = {
            "id": _id(),
            "title": title,
            "body": body,
            "active": active,
            "added": datetime.utcnow().isoformat(),
        }
        announcements.append(entry)
        _save(ANNOUNCEMENTS_FILE, announcements)
        return entry

def update_announcement(announcement_id: str, title=None, body=None, active=None) -> Optional[dict]:
    with _lock:
        announcements = _load(ANNOUNCEMENTS_FILE)
        for a in announcements:
            if a["id"] == announcement_id:
                if title is not None: a["title"] = title
                if body is not None: a["body"] = body
                if active is not None: a["active"] = active
                _save(ANNOUNCEMENTS_FILE, announcements)
                return a
        return None

def remove_announcement(announcement_id: str) -> bool:
    with _lock:
        announcements = _load(ANNOUNCEMENTS_FILE)
        new = [a for a in announcements if a["id"] != announcement_id]
        if len(new) == len(announcements): return False
        _save(ANNOUNCEMENTS_FILE, new)
        return True


# ── Bot-facing helpers ────────────────────────────────────────────────────────

def get_links_block() -> str:
    links = get_links()
    if not links:
        return ""
    lines = ["Reference links:"]
    for l in links:
        desc = f" — {l['description']}" if l.get("description") else ""
        lines.append(f"  {l['label']}: {l['url']}{desc}")
    return "\n".join(lines)


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9_]+", text.lower()))


def find_matching_qa(query: str, top_k: int = 6) -> str:
    """Keyword overlap match for small Q&A sets."""
    pairs = get_qa_pairs()
    if not pairs:
        return ""
    q_words = _tokens(query)
    scored = []
    for p in pairs:
        searchable = " ".join(
            [
                p.get("question", ""),
                " ".join(p.get("tags", [])),
            ]
        )
        overlap = len(_tokens(searchable) & q_words)
        if overlap > 0:
            scored.append((overlap, p))
    scored.sort(key=lambda x: x[0], reverse=True)
    if not scored:
        return ""
    lines = ["Manual Q&A context:"]
    for _, p in scored[:top_k]:
        lines.append(f"Q: {p['question']}")
        lines.append(f"A: {p['answer']}")
        lines.append("")
    return "\n".join(lines).strip()


def find_matching_information(query: str, top_k: int = 8) -> str:
    """Keyword overlap match for standalone information snippets."""
    items = get_information()
    if not items:
        return ""
    q_words = _tokens(query)
    scored = []
    for item in items:
        searchable = item.get("body", "")
        overlap = len(_tokens(searchable) & q_words)
        if overlap > 0:
            scored.append((overlap, item))
    scored.sort(key=lambda x: x[0], reverse=True)
    if not scored:
        return ""
    lines = ["Information context:"]
    for _, item in scored[:top_k]:
        lines.append(f"Body: {item.get('body', '')}")
        lines.append("")
    return "\n".join(lines).strip()


def get_all_qa_block() -> str:
    pairs = get_qa_pairs()
    if not pairs:
        return ""
    lines = ["Manual Q&A context:"]
    for p in pairs:
        tags = f" Tags: {', '.join(p.get('tags', []))}" if p.get("tags") else ""
        lines.append(f"Q: {p['question']}{tags}")
        lines.append(f"A: {p['answer']}")
        lines.append("")
    return "\n".join(lines).strip()


def get_announcements_block() -> str:
    announcements = [a for a in get_announcements() if a.get("active", True)]
    if not announcements:
        return ""
    announcements.sort(key=lambda a: a.get("added", ""), reverse=True)
    lines = ["Active announcements, newest first:"]
    for a in announcements:
        lines.append(f"Title: {a.get('title', '')}")
        if a.get("added"):
            lines.append(f"Date: {a['added'][:10]}")
        lines.append(f"Body: {a.get('body', '')}")
        lines.append("")
    return "\n".join(lines).strip()
