# api/index.py  — solo cambia la inicialización de los DataStores
# Busca estas 2 líneas en tu index.py:
#
#   store_camara = DataStore(DATA_REPO_DIR,   KOM_DIR)
#   store_senado = DataStore(SENADO_REPO_DIR, KOM_DIR)
#
# Y reemplázalas con:
#
#   store_camara = DataStore(DATA_REPO_DIR,   KOM_DIR, default_chamber="camara")
#   store_senado = DataStore(SENADO_REPO_DIR, KOM_DIR, default_chamber="senado")
#
# El resto del archivo queda IGUAL.
# ──────────────────────────────────────────────────────────────
# A continuación el archivo completo por si prefieres reemplazarlo:

from __future__ import annotations

import json
import os
import shutil
import uuid
import mimetypes
from datetime import datetime
from typing import Any, Dict

from fastapi import Body, FastAPI, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles

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

# ★★★ FIX: pasar default_chamber a cada DataStore ★★★
# Cuando un integrante.json no tiene campo "chamber", se asume
# la cámara correspondiente al repo que se está leyendo.
store_camara = DataStore(DATA_REPO_DIR,   KOM_DIR, default_chamber="camara")
store_senado = DataStore(SENADO_REPO_DIR, KOM_DIR, default_chamber="senado")

agent = LegislativeAgent(store_camara, GEMINI_API_KEY)

def get_store(camara: str = "diputados") -> DataStore:
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
        "success":           True,
        "data_repo_dir":     store_camara.data_repo_dir,
        "senado_repo_dir":   SENADO_REPO_DIR,
        "senado_disponible": senado_ok,
        "kom_dir":           store_camara.kom_dir,
        "gemini_configured": bool(GEMINI_API_KEY),
    }


@app.get("/api/commissions")
def commissions(group: str = "Permanentes", q: str = "", camara: str = "diputados"):
    store = get_store(camara)
    comms = store.list_commissions(group, q=q)
    return {
        "success":     True,
        "commissions": comms,
        "items":       comms,
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
        # Combinar ambas cámaras
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
    if camara.lower() == "senado":
        items = store_senado.activity_feed(group=group, status=status, q=q, days_back=days)
    elif camara.lower() in ("diputados", "camara"):
        items = store_camara.activity_feed(group=group, status=status, q=q, days_back=days)
    else:
        items_c = store_camara.activity_feed(group=group, status=status, q=q, days_back=days)
        items_s = store_senado.activity_feed(group=group, status=status, q=q, days_back=days)
        items   = sorted(items_c + items_s, key=lambda x: x.get("Fecha", ""), reverse=True)

    return {"success": True, "items": items, "total": len(items), "days_back": days}
DOCS_SUBDIR = "docs"

def docs_dir(camara: str, group: str, commission_name: str) -> str:
    store = get_store(camara)
    d = os.path.join(store.data_repo_dir, group, commission_name, DOCS_SUBDIR)
    os.makedirs(d, exist_ok=True)
    return d

def docs_meta_path(camara: str, group: str, commission_name: str) -> str:
    return os.path.join(docs_dir(camara, group, commission_name), "docs_meta.json")

def load_docs_meta(camara: str, group: str, commission_name: str) -> list:
    p = docs_meta_path(camara, group, commission_name)
    if not os.path.exists(p):
        return []
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def save_docs_meta(camara: str, group: str, commission_name: str, meta: list):
    p = docs_meta_path(camara, group, commission_name)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


# ── GET: listar documentos de una comisión ──────────────────
@app.get("/api/docs/{camara}/{group}/{commission_name}")
def list_docs(camara: str, group: str, commission_name: str, sesion_fecha: str = "", scope: str = ""):
    """
    Lista documentos de una comisión.
    Filtros opcionales:
      sesion_fecha = "12-11-2024"  → solo docs de esa sesión
      scope        = "sesion" | "comision"
    """
    meta = load_docs_meta(camara, group, commission_name)
    if sesion_fecha:
        meta = [d for d in meta if d.get("sesion_fecha") == sesion_fecha]
    if scope:
        meta = [d for d in meta if d.get("scope") == scope]
    # Ordenar: más recientes primero
    meta.sort(key=lambda x: x.get("uploaded_at", ""), reverse=True)
    return {"success": True, "docs": meta, "total": len(meta)}


# ── POST: subir documento ───────────────────────────────────
@app.post("/api/docs/{camara}/{group}/{commission_name}/upload")
async def upload_doc(
    camara: str, group: str, commission_name: str,
    file: UploadFile = File(...),
    sesion_fecha: str = "",
    scope: str = "sesion",
    tipo: str = "otro",
    title: str = "",
    notas: str = "",
):
    """Sube un archivo y lo registra en docs_meta.json."""
    d = docs_dir(camara, group, commission_name)

    # Nombre único en disco
    ext = os.path.splitext(file.filename or "doc")[-1].lower() or ".bin"
    doc_id = str(uuid.uuid4())[:8]
    safe_date = sesion_fecha.replace("/", "-").replace(" ", "_")
    stored_name = f"{safe_date}_{doc_id}{ext}" if safe_date else f"{doc_id}{ext}"
    dest = os.path.join(d, stored_name)

    raw = await file.read()
    with open(dest, "wb") as f_out:
        f_out.write(raw)

    meta = load_docs_meta(camara, group, commission_name)
    entry = {
        "id":           doc_id,
        "filename":     stored_name,
        "original_name": file.filename or stored_name,
        "title":        title or file.filename or stored_name,
        "tipo":         tipo,
        "sesion_fecha": sesion_fecha,
        "scope":        scope,
        "source":       "manual",
        "url":          "",
        "notas":        notas,
        "size_bytes":   len(raw),
        "uploaded_at":  datetime.utcnow().isoformat() + "Z",
    }
    meta.append(entry)
    save_docs_meta(camara, group, commission_name, meta)

    return {"success": True, "doc": entry}


# ── POST: registrar URL externa (scraping / link) ──────────
@app.post("/api/docs/{camara}/{group}/{commission_name}/link")
def add_doc_link(
    camara: str, group: str, commission_name: str,
    payload: dict = Body(...),
):
    """
    Registra un documento externo por URL (sin subir archivo).
    payload: { url, title, tipo, sesion_fecha, scope, notas, source }
    """
    meta = load_docs_meta(camara, group, commission_name)
    doc_id = str(uuid.uuid4())[:8]
    entry = {
        "id":           doc_id,
        "filename":     "",
        "original_name": payload.get("title", ""),
        "title":        payload.get("title", "Documento externo"),
        "tipo":         payload.get("tipo", "otro"),
        "sesion_fecha": payload.get("sesion_fecha", ""),
        "scope":        payload.get("scope", "sesion"),
        "source":       payload.get("source", "manual"),
        "url":          payload.get("url", ""),
        "notas":        payload.get("notas", ""),
        "size_bytes":   0,
        "uploaded_at":  datetime.utcnow().isoformat() + "Z",
    }
    meta.append(entry)
    save_docs_meta(camara, group, commission_name, meta)
    return {"success": True, "doc": entry}


# ── DELETE: eliminar documento ──────────────────────────────
@app.delete("/api/docs/{camara}/{group}/{commission_name}/{doc_id}")
def delete_doc(camara: str, group: str, commission_name: str, doc_id: str):
    meta = load_docs_meta(camara, group, commission_name)
    entry = next((d for d in meta if d["id"] == doc_id), None)
    if not entry:
        return {"success": False, "error": "Documento no encontrado"}

    # Eliminar archivo físico si existe
    if entry.get("filename"):
        fpath = os.path.join(docs_dir(camara, group, commission_name), entry["filename"])
        if os.path.exists(fpath):
            os.remove(fpath)

    meta = [d for d in meta if d["id"] != doc_id]
    save_docs_meta(camara, group, commission_name, meta)
    return {"success": True}


# ── GET: servir archivo subido ──────────────────────────────
@app.get("/api/docs/{camara}/{group}/{commission_name}/file/{filename}")
def serve_doc_file(camara: str, group: str, commission_name: str, filename: str):
    d    = docs_dir(camara, group, commission_name)
    path = os.path.join(d, filename)
    if not os.path.isfile(path):
        return {"error": "Archivo no encontrado"}
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    return FileResponse(path, media_type=mime, filename=filename)

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
    result = {
        "config": {
            "DATA_REPO_DIR":   DATA_REPO_DIR,   "exists_camara": os.path.isdir(DATA_REPO_DIR),
            "SENADO_REPO_DIR": SENADO_REPO_DIR, "exists_senado": os.path.isdir(SENADO_REPO_DIR),
            "KOM_DIR":         KOM_DIR,
        }
    }
    try:
        comms_c = store_camara.list_commissions("Permanentes")
        comms_s = store_senado.list_commissions("Permanentes")
        pols_c  = store_camara.list_politicians(chamber="camara")
        pols_s  = store_senado.list_politicians(chamber="senado")
        result["datastore"] = {
            "camara_permanentes": len(comms_c),
            "senado_permanentes": len(comms_s),
            "diputados_total":    len(pols_c),
            "senadores_total":    len(pols_s),
        }
    except Exception as e:
        result["datastore_error"] = str(e)
    return result


@app.get("/api/file")
def serve_file(path: str):
    """Sirve archivos del repo para el agente IA."""
    safe_root = os.path.abspath(PROJECT_DIR)
    abs_path  = os.path.abspath(path)
    if not abs_path.startswith(safe_root):
        return {"error": "Acceso denegado"}
    if not os.path.isfile(abs_path):
        return {"error": "Archivo no encontrado"}
    return FileResponse(abs_path)