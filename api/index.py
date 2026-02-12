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
from fastapi.responses import PlainTextResponse
from fastapi import FastAPI
from fastapi import Body
from fastapi import HTTPException


# Imports relativos corregidos
import datastore
import agent

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

store = datastore(DATA_REPO_DIR, KOM_DIR)
agent = agent(store, GEMINI_API_KEY)

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
@app.get("/api/session_txt")
def session_txt(group: str, commission: str, session_id: str):
    # ejemplo esperado:
    # data_repo_dir/Permanentes/Agricultura, Silvicultura y Desarrollo Rural/221.txt
    base = store.data_repo_dir
    if not base:
        raise HTTPException(500, "data_repo_dir no configurado")

    commission_dir = os.path.join(base, group, commission)
    path = os.path.join(commission_dir, f"{session_id}.txt")

    if not os.path.exists(path):
        raise HTTPException(404, "TXT no encontrado para esa sesión")

    try:
        with open(path, "r", encoding="utf-8") as f:
            return PlainTextResponse(f.read())
    except Exception as e:
        raise HTTPException(500, f"No se pudo leer TXT: {e}")

@app.get("/api/kom/{slug}")
def kom_by_slug(slug: str):
    prof = store.get_kom_profile(slug)  # (lo implementamos abajo)
    if not prof:
        raise HTTPException(status_code=404, detail="KOM profile not found")
    return {"success": True, "profile": prof}

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
        "commissions": comms,
        "total": len(comms)
    }


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
def activity(
    group: str = "", 
    status: str = "", 
    q: str = "",
    days: int = 90  # Nuevo parámetro: días hacia atrás (default: 90 = 3 meses)
):
    """
    Obtiene actividad legislativa reciente
    
    Query params:
        - group: Filtrar por grupo (Permanentes, Otras, Unidas)
        - status: Filtrar por estado (CITADA, CELEBRADA, SUSPENDIDA, etc)
        - q: Búsqueda en nombre de comisión
        - days: Días hacia atrás (default 90 = 3 meses)
                30 = último mes
                180 = últimos 6 meses
                365 = último año
    
    Ejemplo: /api/activity?status=CITADA&days=30
    """
    items = store.activity_feed(
        group=group, 
        status=status, 
        q=q,
        days_back=days
    )
    return {
        "success": True, 
        "items": items,
        "total": len(items),
        "days_back": days  # Informar cuántos días se filtraron
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