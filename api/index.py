# api/index.py
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Dict

from fastapi import Body, FastAPI, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi import FastAPI

# Imports relativos corregidos
from .datastore import DataStore
from .agent import LegislativeAgent

# ---------------- config ----------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.abspath(os.path.join(BASE_DIR, ".."))

# Load environment variables
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(PROJECT_DIR, ".env"))
except ImportError:
    print("Warning: python-dotenv not installed, using system environment variables")

DATA_REPO_DIR = os.getenv("DATA_REPO_DIR") or os.path.join(PROJECT_DIR, "REPO_V40_HISTORIAL_COMPLETO_V2")
KOM_DIR = os.getenv("KOM_DIR") or os.path.join(PROJECT_DIR, "KOM")
PUBLIC_DIR = os.path.join(PROJECT_DIR, "public")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

store = DataStore(DATA_REPO_DIR, KOM_DIR)
agent = LegislativeAgent(store, GEMINI_API_KEY)

app = FastAPI(title="Observatorio Politico API", version="0.2")

# CORS (frontend served from same origin usually; keep permissive for dev)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve frontend
if os.path.isdir(PUBLIC_DIR):
    app.mount("/public", StaticFiles(directory=PUBLIC_DIR, html=True), name="public")


# ---------------- helpers ----------------
def kom_profile_path(chamber: str, pid: str) -> str:
    chamber = (chamber or "camara").lower()
    safe_pid = str(pid).strip()
    base = os.path.join(store.kom_dir, "profiles", chamber)
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, f"{safe_pid}.json")


# ---------------- endpoints ----------------

@app.get("/health")
def health():
    return {"ok": True}

@app.get("/")
def root():
    """Redirect to index.html"""
    index_path = os.path.join(PUBLIC_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"message": "Observatorio Político API", "version": "0.2"}


@app.get("/api/health")
def health():
    return {
        "success": True,
        "data_repo_dir": store.data_repo_dir,
        "kom_dir": store.kom_dir,
        "gemini_configured": bool(GEMINI_API_KEY),
    }


@app.get("/api/commissions")
def commissions(group: str = "Permanentes", q: str = ""):
    comms = store.list_commissions(group, q=q)
    return {
        "success": True, 
        "items": comms,  # ← CAMBIO: era "commissions", ahora "items"
        "total": len(comms),
        "group": group
    }


@app.get("/api/test-debug")
def test_debug():
    """Endpoint de debug para verificar configuración"""
    import os
    
    repo = DATA_REPO_DIR
    perm_dir = os.path.join(repo, "Permanentes")
    
    result = {
        "config": {
            "DATA_REPO_DIR": repo,
            "KOM_DIR": KOM_DIR,
            "exists_repo": os.path.isdir(repo),
            "exists_perm": os.path.isdir(perm_dir),
        }
    }
    
    if os.path.isdir(perm_dir):
        try:
            items = os.listdir(perm_dir)
            dirs = [i for i in items if os.path.isdir(os.path.join(perm_dir, i))]
            
            result["filesystem"] = {
                "total_items": len(items),
                "total_dirs": len(dirs),
                "first_5_dirs": sorted(dirs)[:5]
            }
        except Exception as e:
            result["filesystem_error"] = str(e)
    
    try:
        comms = store.list_commissions("Permanentes", q="")
        result["datastore"] = {
            "total_commissions": len(comms),
            "first_3": [c["commission_name"] for c in comms[:3]]
        }
    except Exception as e:
        result["datastore_error"] = str(e)
    
    return result


@app.get("/api/commissions/{group}/{commission_name}/sessions")
def commission_sessions(group: str, commission_name: str):
    return store.get_commission_sessions(group, commission_name)


@app.get("/api/commissions/{group}/{commission_name}/sessions/{sid}/transcript")
def get_transcript(group: str, commission_name: str, sid: str):
    path = store.find_transcript_path(group, commission_name, sid)
    if not path:
        return {"success": False, "error": "Transcript no encontrado"}

    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return {"success": True, "text": f.read()}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.get("/api/politicians")
def politicians(q: str = ""):
    pols = store.list_politicians(q=q)
    return {
        "success": True, 
        "politicians": pols,
        "total": len(pols)
    }


@app.get("/api/activity")
def activity(group: str = "", status: str = "", q: str = "", days: int = 90):
    items = store.activity_feed(group=group, status=status, q=q, days_back=days)
    return {
        "success": True, 
        "items": items,
        "total": len(items),
        "days_back": days
    }


@app.get("/api/news")
def news(source: str = "diario_oficial", q: str = ""):
    items = store.news_feed(source=source, q=q)
    return {
        "success": True, 
        "items": items,
        "total": len(items)
    }


# ---- KOM profiles ----
@app.get("/api/kom/{chamber}/{pid}")
def get_kom_profile(chamber: str, pid: str):
    path = kom_profile_path(chamber, pid)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return {"success": True, "exists": True, "profile": json.load(f)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    return {
        "success": True,
        "exists": False,
        "profile": {
            "id": pid,
            "chamber": chamber,
            "tags": [],
            "notes": "",
            "notas": "",
            "links": [],
            "updated_at": None,
        },
    }


@app.post("/api/kom/{chamber}/{pid}")
def save_kom_profile(chamber: str, pid: str, payload: dict = Body(...)):
    payload["id"] = pid
    payload["chamber"] = chamber
    payload["updated_at"] = datetime.utcnow().isoformat() + "Z"

    path = kom_profile_path(chamber, pid)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return {"success": True, "saved": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ---- Upload ----
@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    try:
        raw = await file.read()
        saved_as = store.save_upload(file.filename, raw)
        return {"success": True, "saved_as": saved_as}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ---- Chat with AI agent ----
@app.post("/api/chat")
def chat(payload: dict = Body(...)):
    msg = (payload or {}).get("message") or ""
    if not msg:
        return {"success": False, "error": "No message provided"}
    
    try:
        response = agent.ask(msg)
        return {"success": True, "response": response}
    except Exception as e:
        return {"success": False, "error": str(e), "response": f"Error: {str(e)}"}