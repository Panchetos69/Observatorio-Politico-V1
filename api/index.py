# api/index.py
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from fastapi import Body, FastAPI, File, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

# ------------------------------------------------------------
# Paths
# ------------------------------------------------------------
ROOT_PATH = Path(__file__).parent.parent  # raíz del proyecto
API_DIR = Path(__file__).parent
sys.path.append(str(API_DIR))  # permite imports locales tipo: from datastore import DataStore

# Imports locales (api/)
from datastore import DataStore
from agent import LegislativeAgent

# ------------------------------------------------------------
# Env / Config
# ------------------------------------------------------------
BASE_DIR = str(ROOT_PATH)
PROJECT_DIR = str(ROOT_PATH)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(PROJECT_DIR, ".env"))
except ImportError:
    print("Warning: python-dotenv not installed, using system environment variables")

DATA_REPO_DIR = os.getenv("DATA_REPO_DIR") or os.path.join(PROJECT_DIR, "REPO_V40_HISTORIAL_COMPLETO_V2")
KOM_DIR = os.getenv("KOM_DIR") or os.path.join(PROJECT_DIR, "KOM")
PUBLIC_DIR = os.path.join(PROJECT_DIR, "public")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# Detectar Vercel/Lambda (filesystem read-only salvo /tmp)
IS_SERVERLESS = bool(os.getenv("VERCEL") or os.getenv("AWS_LAMBDA_FUNCTION_NAME"))

store = DataStore(DATA_REPO_DIR, KOM_DIR)
agent = LegislativeAgent(store, GEMINI_API_KEY)

app = FastAPI(title="Observatorio Politico API", version="0.2")

# ------------------------------------------------------------
# Static (solo si existe /public en runtime)
# En Vercel normalmente el estático lo sirve Vercel por fuera, pero esto no molesta localmente.
# ------------------------------------------------------------
if os.path.isdir(PUBLIC_DIR):
    app.mount("/public", StaticFiles(directory=PUBLIC_DIR, html=True), name="public")

# ------------------------------------------------------------
# CORS (dev friendly)
# ------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------
def _safe_mkdir(path: str):
    """Crear directorio solo si NO estamos en serverless (o usar /tmp)."""
    if IS_SERVERLESS:
        return
    os.makedirs(path, exist_ok=True)

def kom_profile_path(chamber: str, pid: str) -> str:
    chamber = (chamber or "camara").lower()
    safe_pid = str(pid).strip()

    # En serverless => /tmp (escribible)
    if IS_SERVERLESS:
        base = os.path.join("/tmp", "kom_profiles", chamber)
        os.makedirs(base, exist_ok=True)  # /tmp sí permite
        return os.path.join(base, f"{safe_pid}.json")

    # Local => carpeta KOM/profiles/...
    base = os.path.join(store.kom_dir, "profiles", chamber)
    _safe_mkdir(base)
    return os.path.join(base, f"{safe_pid}.json")

# ------------------------------------------------------------
# Health
# ------------------------------------------------------------
@app.get("/health")
def health():
    return {"ok": True}

@app.get("/api/health")
def api_health():
    return {"ok": True}

# ------------------------------------------------------------
# Front root: devolver index.html si existe
# (En Vercel normalmente no se usa, pero no rompe local)
# ------------------------------------------------------------
@app.get("/")
async def read_index():
    # Busca /public/index.html
    index_path = os.path.join(PUBLIC_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)

    # fallback: raíz
    fallback = os.path.join(PROJECT_DIR, "index.html")
    if os.path.exists(fallback):
        return FileResponse(fallback)

    return {"error": "No encontré index.html", "buscado_en": [index_path, fallback]}

# ------------------------------------------------------------
# TXT por sesión
# ------------------------------------------------------------
@app.get("/api/session_txt")
def session_txt(group: str, commission: str, sid: str):
    base = store.data_repo_dir
    if not base:
        raise HTTPException(500, "data_repo_dir no configurado")

    commission_dir = os.path.join(base, group, commission)
    path = os.path.join(commission_dir, f"{sid}.txt")

    if not os.path.exists(path):
        raise HTTPException(404, "TXT no encontrado para esa sesión")

    try:
        with open(path, "r", encoding="utf-8") as f:
            return PlainTextResponse(f.read())
    except Exception as e:
        raise HTTPException(500, f"No se pudo leer TXT: {e}")

# ------------------------------------------------------------
# Comisiones / sesiones / transcript
# ------------------------------------------------------------
@app.get("/api/commissions")
def commissions(group: str = "Permanentes", q: str = ""):
    return store.list_commissions(group, q=q)

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

# ------------------------------------------------------------
# Politicians
# ------------------------------------------------------------
@app.get("/api/politicians")
def politicians(q: str = ""):
    pols = store.list_politicians(q=q)
    return {"success": True, "politicians": pols, "total": len(pols)}

# ------------------------------------------------------------
# Activity
# ------------------------------------------------------------
@app.get("/api/activity")
def activity(group: str = "", status: str = "", q: str = "", days: int = 90):
    items = store.activity_feed(group=group, status=status, q=q, days_back=days)
    return {"success": True, "items": items, "total": len(items), "days_back": days}

# ------------------------------------------------------------
# News
# ------------------------------------------------------------
@app.get("/api/news")
def news(source: str = "diario_oficial", q: str = ""):
    items = store.news_feed(source=source, q=q)
    return {"success": True, "items": items, "total": len(items)}

# ------------------------------------------------------------
# KOM profiles
# ------------------------------------------------------------
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
        # En serverless solo /tmp
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return {"success": True, "saved": True, "path": path if IS_SERVERLESS else None}
    except Exception as e:
        return {"success": False, "error": str(e)}

# ------------------------------------------------------------
# Upload
# (OJO: si tu store.save_upload escribe en disco, en Vercel debe usar /tmp)
# ------------------------------------------------------------
@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    try:
        raw = await file.read()
        saved_as = store.save_upload(file.filename, raw)
        return {"success": True, "saved_as": saved_as}
    except Exception as e:
        return {"success": False, "error": str(e)}

# ------------------------------------------------------------
# Chat IA
# ------------------------------------------------------------
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