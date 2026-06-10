"""
admin_ui/server.py — FastAPI backend for the web admin UI.
Serves the HTML page and REST endpoints for links and Q&A management.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from data.knowledge_store import (
    get_links, add_link, update_link, remove_link,
    get_qa_pairs, add_qa, update_qa, remove_qa,
    get_information, add_information, update_information, remove_information,
    get_announcements, add_announcement, update_announcement, remove_announcement,
)
from data.wandb_service import wandb_cache

ADMIN_TOKEN = os.getenv("ADMIN_UI_TOKEN", "changeme")

app = FastAPI(title="Subnet Bot Admin", docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

UI_FILE = Path(__file__).parent / "static" / "index.html"


def _auth(x_admin_token: str = Header(...)):
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid token")


# ── Status ────────────────────────────────────────────────────────────────────

@app.get("/api/status")
def status(_=Depends(_auth)):
    return {
        "wandb_last_updated": wandb_cache.last_updated,
        "wandb_stale":        wandb_cache.is_stale,
        "wandb_active_runs":  wandb_cache.active_run_ids,
        "top_miners":         wandb_cache.get_top_miners(5),
        "link_count":         len(get_links()),
        "qa_count":           len(get_qa_pairs()),
        "information_count":  len(get_information()),
        "announcement_count": len(get_announcements()),
    }


# ── Links ─────────────────────────────────────────────────────────────────────

class LinkIn(BaseModel):
    label: str
    url: str
    description: str = ""

class LinkPatch(BaseModel):
    label: Optional[str] = None
    url: Optional[str] = None
    description: Optional[str] = None

@app.get("/api/links")
def list_links(_=Depends(_auth)):
    return get_links()

@app.post("/api/links", status_code=201)
def create_link(body: LinkIn, _=Depends(_auth)):
    return add_link(body.label, body.url, body.description)

@app.patch("/api/links/{lid}")
def edit_link(lid: str, body: LinkPatch, _=Depends(_auth)):
    result = update_link(lid, body.label, body.url, body.description)
    if not result:
        raise HTTPException(404, "Link not found")
    return result

@app.delete("/api/links/{lid}")
def delete_link(lid: str, _=Depends(_auth)):
    if not remove_link(lid):
        raise HTTPException(404, "Link not found")
    return {"ok": True}


# ── Q&A ───────────────────────────────────────────────────────────────────────

class QAIn(BaseModel):
    question: str
    answer: str
    tags: List[str] = []

class QAPatch(BaseModel):
    question: Optional[str] = None
    answer: Optional[str] = None
    tags: Optional[List[str]] = None

@app.get("/api/qa")
def list_qa(_=Depends(_auth)):
    return get_qa_pairs()

@app.post("/api/qa", status_code=201)
def create_qa(body: QAIn, _=Depends(_auth)):
    return add_qa(body.question, body.answer, body.tags)

@app.patch("/api/qa/{qid}")
def edit_qa(qid: str, body: QAPatch, _=Depends(_auth)):
    result = update_qa(qid, body.question, body.answer, body.tags)
    if not result:
        raise HTTPException(404, "Q&A not found")
    return result

@app.delete("/api/qa/{qid}")
def delete_qa(qid: str, _=Depends(_auth)):
    if not remove_qa(qid):
        raise HTTPException(404, "Q&A not found")
    return {"ok": True}


# ── Information ───────────────────────────────────────────────────────────────

class InformationIn(BaseModel):
    body: str

class InformationPatch(BaseModel):
    body: Optional[str] = None

@app.get("/api/information")
def list_information(_=Depends(_auth)):
    return get_information()

@app.post("/api/information", status_code=201)
def create_information(body: InformationIn, _=Depends(_auth)):
    return add_information(body.body)

@app.patch("/api/information/{iid}")
def edit_information(iid: str, body: InformationPatch, _=Depends(_auth)):
    result = update_information(iid, body.body)
    if not result:
        raise HTTPException(404, "Information not found")
    return result

@app.delete("/api/information/{iid}")
def delete_information(iid: str, _=Depends(_auth)):
    if not remove_information(iid):
        raise HTTPException(404, "Information not found")
    return {"ok": True}


# ── Announcements ─────────────────────────────────────────────────────────────

class AnnouncementIn(BaseModel):
    title: str
    body: str
    active: bool = True

class AnnouncementPatch(BaseModel):
    title: Optional[str] = None
    body: Optional[str] = None
    active: Optional[bool] = None

@app.get("/api/announcements")
def list_announcements(_=Depends(_auth)):
    return get_announcements()

@app.post("/api/announcements", status_code=201)
def create_announcement(body: AnnouncementIn, _=Depends(_auth)):
    return add_announcement(body.title, body.body, body.active)

@app.patch("/api/announcements/{aid}")
def edit_announcement(aid: str, body: AnnouncementPatch, _=Depends(_auth)):
    result = update_announcement(aid, body.title, body.body, body.active)
    if not result:
        raise HTTPException(404, "Announcement not found")
    return result

@app.delete("/api/announcements/{aid}")
def delete_announcement(aid: str, _=Depends(_auth)):
    if not remove_announcement(aid):
        raise HTTPException(404, "Announcement not found")
    return {"ok": True}


# ── Serve UI ──────────────────────────────────────────────────────────────────

@app.get("/")
def serve_ui():
    return FileResponse(UI_FILE)
