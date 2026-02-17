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

from .datastore import DataStore
from .agent import LegislativeAgent

# ---------------- config ----------------
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.abspath(os.path.join(BASE_DIR, ".."))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(PROJECT_DIR, ".env"))
except ImportError:
    pass

DATA_REPO_DIR    = os.getenv("DATA_REPO_DIR")    or os.path.join(PROJECT_DIR, "REPO_V40_HISTORIAL_COMPLETO_V2")
SENADO_REPO_DIR  = os.getenv("SENADO_REPO_DIR")  or os.path.join(PROJECT_DIR, "REPO_SENADO")
KOM_DIR          = os.getenv("KOM_DIR")          or os.path.join(PROJECT_DIR, "KOM")
PUBLIC_DIR       = os.path.join(PROJECT_DIR, "public")
GEMINI_API_KEY   = os.getenv("GEMINI_API_KEY", "")

# Dos DataStores — uno por cámara
store_camara = DataStore(DATA_REPO_DIR,   KOM_DIR)
store_senado = DataStore(SENADO_REPO_DIR, KOM_DIR)

agent = LegislativeAgent(store_camara, GEMINI_API_KEY)

def get_store(camara: str = "diputados") -> DataStore:
    """Devuelve el DataStore correcto según parámetro ?camara="""
    return store_senado if camara.lower() in ("senado", "senate") else store_camara

app = FastAPI(title="Observatorio Politico API", version="0.3")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if os.path.isdir(PUBLIC_DIR):
    app.mount("/public", StaticFiles(directory=PUBLIC_DIR, html=True), name="public")


# ---------------- helpers ----------------
def kom_profile_path(chamber: str, pid: str) -> str:
    chamber = (chamber or "camara").lower()
    base = os.path.join(store_camara.kom_dir, "profiles", chamber)
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, f"{str(pid).strip()}.json")


# ---------------- endpoints ----------------

@app.get("/health")
def health_simple():
    return {"ok": True}

@app.get("/")
def root():
    index_path = os.path.join(PUBLIC_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"message": "Observatorio Político API", "version": "0.3"}


@app.get("/api/health")
def health():
    senado_ok = os.path.isdir(SENADO_REPO_DIR)
    return {
        "success":          True,
        "data_repo_dir":    store_camara.data_repo_dir,
        "senado_repo_dir":  SENADO_REPO_DIR,
        "senado_disponible": senado_ok,
        "kom_dir":          store_camara.kom_dir,
        "gemini_configured": bool(GEMINI_API_KEY),
    }


@app.get("/api/commissions")
def commissions(group: str = "Permanentes", q: str = "", camara: str = "diputados"):
    """
    Parámetros:
      group  = Permanentes | Otras | Unidas
      q      = búsqueda por nombre
      camara = diputados | senado
    """
    store = get_store(camara)
    comms = store.list_commissions(group, q=q)
    return {
        "success":     True,
        "commissions": comms,
        "items":       comms,   # compatibilidad con versiones anteriores
        "total":       len(comms),
        "group":       group,
        "camara":      camara,
    }


@app.get("/api/commissions/{group}/{commission_name}/sessions")
def commission_sessions(group: str, commission_name: str, camara: str = "diputados"):
    store = get_store(camara)
    return store.get_commission_sessions(group, commission_name)


@app.get("/api/commissions/{group}/{commission_name}/sessions/{sid}/transcript")
def get_transcript(group: str, commission_name: str, sid: str, camara: str = "diputados"):
    store = get_store(camara)
    path  = store.find_transcript_path(group, commission_name, sid)
    if not path:
        return {"success": False, "error": "Transcript no encontrado"}
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return {"success": True, "text": f.read()}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.get("/api/politicians")
def politicians(q: str = "", camara: str = "all"):
    """
    camara = "all"       -> combina Diputados + Senado
    camara = "camara"    -> solo Diputados
    camara = "diputados" -> solo Diputados
    camara = "senado"    -> solo Senado
    """
    c = (camara or "all").lower()

    if c in ("senado", "senate"):
        pols = store_senado.list_politicians(q=q, chamber="senado")

    elif c in ("camara", "diputados", "diputado"):
        pols = store_camara.list_politicians(q=q, chamber="camara")

    else:
        # Combinar ambas camaras
        pols_c = store_camara.list_politicians(q=q, chamber="camara")
        pols_s = store_senado.list_politicians(q=q, chamber="senado")
        seen = set()
        pols = []
        for p in pols_c + pols_s:
            key = f"{p.get('chamber','')}::{p.get('nombre','')}"
            if key not in seen:
                seen.add(key)
                pols.append(p)
        pols.sort(key=lambda x: x.get("nombre", ""))

    return {"success": True, "politicians": pols, "total": len(pols)}


@app.get("/api/activity")
def activity(group: str = "", status: str = "", q: str = "",
             days: int = 90, camara: str = ""):
    """
    camara = "" → actividad de ambas cámaras (mezclada)
    """
    if camara.lower() == "senado":
        items = store_senado.activity_feed(group=group, status=status, q=q, days_back=days)
    elif camara.lower() in ("diputados","camara"):
        items = store_camara.activity_feed(group=group, status=status, q=q, days_back=days)
    else:
        items_c = store_camara.activity_feed(group=group, status=status, q=q, days_back=days)
        items_s = store_senado.activity_feed(group=group, status=status, q=q, days_back=days)
        items   = sorted(items_c + items_s,
                         key=lambda x: x.get("Fecha",""), reverse=True)

    return {"success": True, "items": items, "total": len(items), "days_back": days}


@app.get("/api/news")
def news(source: str = "diario_oficial", q: str = ""):
    items = store_camara.news_feed(source=source, q=q)
    return {"success": True, "items": items, "total": len(items)}


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
        "success": True, "exists": False,
        "profile": {
            "id": pid, "chamber": chamber, "tags": [],
            "notes": "", "notas": "", "links": [], "updated_at": None,
        },
    }


@app.post("/api/kom/{chamber}/{pid}")
def save_kom_profile(chamber: str, pid: str, payload: dict = Body(...)):
    payload["id"]         = pid
    payload["chamber"]    = chamber
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
        raw      = await file.read()
        saved_as = store_camara.save_upload(file.filename, raw)
        return {"success": True, "saved_as": saved_as}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ---- Chat ----
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


# ---- Debug ----
@app.get("/api/test-debug")
def test_debug():
    repo  = DATA_REPO_DIR
    repis = SENADO_REPO_DIR
    perm  = os.path.join(repo, "Permanentes")
    result = {
        "config": {
            "DATA_REPO_DIR":   repo,   "exists_camara": os.path.isdir(repo),
            "SENADO_REPO_DIR": repis,  "exists_senado": os.path.isdir(repis),
            "KOM_DIR":         KOM_DIR,
        }
    }
    if os.path.isdir(perm):
        dirs = [i for i in os.listdir(perm)
                if os.path.isdir(os.path.join(perm, i))]
        result["camara_permanentes"] = {"total": len(dirs), "muestra": sorted(dirs)[:3]}
    try:
        comms_c = store_camara.list_commissions("Permanentes")
        comms_s = store_senado.list_commissions("Permanentes")
        result["datastore"] = {
            "camara_permanentes":  len(comms_c),
            "senado_permanentes":  len(comms_s),
        }
    except Exception as e:
        result["datastore_error"] = str(e)
    return result