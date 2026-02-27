from __future__ import annotations
import json
import os
import uuid
import mimetypes
from datetime import datetime
from typing import Any, Dict

from fastapi import Body, FastAPI, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .datastore import DataStore
from .agent import LegislativeAgent

# ─────────────────────────────────────────
# Config
# ─────────────────────────────────────────
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.abspath(os.path.join(BASE_DIR, ".."))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(PROJECT_DIR, ".env"))
except ImportError:
    pass

DATA_REPO_DIR   = os.getenv("DATA_REPO_DIR")   or os.path.join(PROJECT_DIR, "REPO_V40_HISTORIAL_COMPLETO_V2")
SENADO_REPO_DIR = os.getenv("SENADO_REPO_DIR") or os.path.join(PROJECT_DIR, "REPO_SENADO")
KOM_DIR         = os.getenv("KOM_DIR")         or os.path.join(PROJECT_DIR, "KOM")
PUBLIC_DIR      = os.path.join(PROJECT_DIR, "public")
GEMINI_API_KEY  = os.getenv("GEMINI_API_KEY", "")
BLOB_TOKEN      = os.getenv("BLOB_READ_WRITE_TOKEN", "")

store_camara = DataStore(DATA_REPO_DIR,   KOM_DIR, default_chamber="camara")
store_senado = DataStore(SENADO_REPO_DIR, KOM_DIR, default_chamber="senado")

agent = LegislativeAgent(store_camara, GEMINI_API_KEY)

def get_store(camara: str = "diputados") -> DataStore:
    return store_senado if (camara or "").lower() in ("senado", "senate") else store_camara


# ─────────────────────────────────────────
# Vercel Blob helpers
# SDK: vercel_blob.put(path, data, options)
#      vercel_blob.list(options)  → {blobs:[{url,pathname}]}
# Para LEER contenido: requests.get(blob_url)
# ─────────────────────────────────────────
try:
    import vercel_blob
    import requests as _requests
    BLOB_AVAILABLE = True
except ImportError:
    BLOB_AVAILABLE = False
    print("[WARN] vercel-blob not installed — falling back to local disk")


def _blob_put(path: str, data: bytes, content_type: str = "application/json") -> dict:
    """Sube datos a Vercel Blob. Devuelve el dict con 'url'."""
    return vercel_blob.put(
        path, data,
        options={"token": BLOB_TOKEN, "contentType": content_type,
                 "access": "public", "addRandomSuffix": "false"},
    )


def _blob_read(path: str) -> bytes | None:
    """Lee el contenido de un blob por su pathname. Devuelve bytes o None."""
    try:
        result = vercel_blob.list(options={"token": BLOB_TOKEN, "prefix": path, "limit": 1})
        blobs  = result.get("blobs", [])
        if not blobs:
            return None
        url = blobs[0]["url"]
        resp = _requests.get(url, timeout=10)
        resp.raise_for_status()
        return resp.content
    except Exception:
        return None


def _blob_meta_key(camara: str, group: str, commission_name: str) -> str:
    return f"docs/{camara}/{group}/{commission_name}/meta.json"


def _blob_file_key(camara: str, group: str, commission_name: str,
                   doc_id: str, ext: str) -> str:
    return f"docs/{camara}/{group}/{commission_name}/files/{doc_id}{ext}"


def load_docs_meta(camara: str, group: str, commission_name: str) -> list:
    """Lee metadata de documentos desde Blob (o disco local)."""
    if BLOB_AVAILABLE and BLOB_TOKEN:
        raw = _blob_read(_blob_meta_key(camara, group, commission_name))
        if raw:
            try:
                return json.loads(raw)
            except Exception:
                pass
        return []
    # Fallback disco local
    store = get_store(camara)
    p = os.path.join(store.data_repo_dir, group, commission_name, "docs", "docs_meta.json")
    if not os.path.exists(p):
        return []
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_docs_meta(camara: str, group: str, commission_name: str, meta: list) -> None:
    """Guarda metadata de documentos en Blob (o disco local)."""
    raw = json.dumps(meta, ensure_ascii=False, indent=2).encode("utf-8")
    if BLOB_AVAILABLE and BLOB_TOKEN:
        _blob_put(_blob_meta_key(camara, group, commission_name), raw)
    else:
        store = get_store(camara)
        d = os.path.join(store.data_repo_dir, group, commission_name, "docs")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "docs_meta.json"), "w", encoding="utf-8") as f:
            f.write(raw.decode("utf-8"))


# ─────────────────────────────────────────
# FastAPI app
# ─────────────────────────────────────────
app = FastAPI(title="Observatorio Político API", version="0.5")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if os.path.isdir(PUBLIC_DIR):
    app.mount("/public", StaticFiles(directory=PUBLIC_DIR, html=True), name="public")


# ─────────────────────────────────────────
# KOM Blob helpers  (ÚNICA implementación)
# ─────────────────────────────────────────
def _kom_blob_key(chamber: str, pid: str) -> str:
    chamber = (chamber or "camara").lower().strip("/")
    pid     = str(pid).strip().strip("/")
    return f"kom/profiles/{chamber}/{pid}.json"


def _load_kom(chamber: str, pid: str) -> dict:
    """Lee perfil KOM desde Blob usando list+GET (SDK no tiene .get())."""
    empty = {
        "id": pid, "chamber": chamber,
        "tags": [], "notas": "", "links": [], "photo": "", "updated_at": None,
    }
    if BLOB_AVAILABLE and BLOB_TOKEN:
        raw = _blob_read(_kom_blob_key(chamber, pid))
        if raw:
            try:
                return json.loads(raw)
            except Exception:
                pass
        return empty
    # Fallback disco local (desarrollo sin Blob)
    path = os.path.join(store_camara.kom_dir, "profiles", chamber.lower(), f"{str(pid).strip()}.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return empty


def _save_kom(chamber: str, pid: str, payload: dict) -> str:
    """Guarda perfil KOM en Blob.
    Si la foto es base64 grande, se guarda como blob separado y se reemplaza por URL.
    """
    # Separar foto base64 → blob independiente
    photo = payload.get("photo", "")
    if photo and photo.startswith("data:") and len(photo) > 500:
        try:
            import base64
            header, b64data = photo.split(",", 1)
            ext = ".png" if "png" in header else ".jpg"
            img_bytes = base64.b64decode(b64data)
            photo_key = f"kom/photos/{chamber}/{pid}{ext}"
            photo_result = _blob_put(photo_key, img_bytes,
                                     content_type="image/png" if ext == ".png" else "image/jpeg")
            payload["photo"] = photo_result.get("url", "")
        except Exception:
            payload["photo"] = ""

    raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")

    if BLOB_AVAILABLE and BLOB_TOKEN:
        result = _blob_put(_kom_blob_key(chamber, pid), raw)
        return result.get("url", "")
    # Fallback disco local
    base = os.path.join(store_camara.kom_dir, "profiles", chamber.lower())
    os.makedirs(base, exist_ok=True)
    path = os.path.join(base, f"{str(pid).strip()}.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write(raw.decode("utf-8"))
    return path


# ─────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────

@app.get("/health")
def health_simple():
    return {"ok": True}


@app.get("/")
def root():
    index_path = os.path.join(PUBLIC_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"message": "Observatorio Político API", "version": "0.5"}


_HTML_PAGES = ["perfil", "comisiones", "congresistas", "actividad", "login"]

@app.get("/{page}.html")
def serve_html_page(page: str):
    if page not in _HTML_PAGES:
        return {"error": "Página no encontrada"}
    path = os.path.join(PUBLIC_DIR, f"{page}.html")
    if os.path.exists(path):
        return FileResponse(path, media_type="text/html")
    return {"error": f"{page}.html no encontrado en public/"}


@app.get("/api/health")
def health():
    return {
        "success":           True,
        "data_repo_dir":     store_camara.data_repo_dir,
        "senado_repo_dir":   SENADO_REPO_DIR,
        "senado_disponible": os.path.isdir(SENADO_REPO_DIR),
        "kom_dir":           store_camara.kom_dir,
        "gemini_configured": bool(GEMINI_API_KEY),
        "blob_configured":   bool(BLOB_TOKEN),
        "blob_sdk":          BLOB_AVAILABLE,
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
    c = (camara or "all").lower()
    if c in ("senado", "senate"):
        pols = store_senado.list_politicians(q=q, chamber="senado")
    elif c in ("camara", "diputados", "diputado"):
        pols = store_camara.list_politicians(q=q, chamber="camara")
    else:
        pols_c = store_camara.list_politicians(q=q, chamber="camara")
        pols_s = store_senado.list_politicians(q=q, chamber="senado")
        seen, pols = set(), []
        for p in pols_c + pols_s:
            key = f"{p.get('chamber','')}::{p.get('nombre','')}"
            if key not in seen:
                seen.add(key)
                pols.append(p)
        pols.sort(key=lambda x: x.get("nombre", ""))
    return {"success": True, "politicians": pols, "total": len(pols)}


@app.get("/api/activity")
def activity(group: str = "", status: str = "", q: str = "",
             days: int = 180, camara: str = ""):
    c = (camara or "").lower()

    def _fecha_key(x: dict):
        from datetime import datetime as _dt
        f = x.get("fecha", "") or x.get("Fecha", "")
        parts = f.split("-")
        if len(parts) == 3:
            try:
                if len(parts[0]) == 4:
                    return _dt(int(parts[0]), int(parts[1]), int(parts[2]))
                else:
                    return _dt(int(parts[2]), int(parts[1]), int(parts[0]))
            except Exception:
                pass
        return _dt.min

    if c == "senado":
        items = store_senado.activity_feed(group=group, status=status, q=q, days_back=days)
    elif c in ("diputados", "camara"):
        items = store_camara.activity_feed(group=group, status=status, q=q, days_back=days)
    else:
        items_c = store_camara.activity_feed(group=group, status=status, q=q, days_back=days)
        items_s = store_senado.activity_feed(group=group, status=status, q=q, days_back=days)
        items   = sorted(items_c + items_s, key=_fecha_key, reverse=True)

    return {"success": True, "items": items, "total": len(items), "days_back": days}


# ─────────────────────────────────────────
# Documentos — Vercel Blob
# ─────────────────────────────────────────

@app.get("/api/docs/{camara}/{group}/{commission_name}")
def list_docs(camara: str, group: str, commission_name: str,
              sesion_fecha: str = "", scope: str = ""):
    meta = load_docs_meta(camara, group, commission_name)
    if sesion_fecha:
        meta = [d for d in meta if d.get("sesion_fecha") == sesion_fecha]
    if scope:
        meta = [d for d in meta if d.get("scope") == scope]
    meta.sort(key=lambda x: x.get("uploaded_at", ""), reverse=True)
    return {"success": True, "docs": meta, "total": len(meta)}


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
    raw    = await file.read()
    ext    = os.path.splitext(file.filename or "doc")[-1].lower() or ".bin"
    doc_id = str(uuid.uuid4())[:8]
    mime   = mimetypes.guess_type(file.filename or "doc")[0] or "application/octet-stream"
    blob_url = ""

    if BLOB_AVAILABLE and BLOB_TOKEN:
        key      = _blob_file_key(camara, group, commission_name, doc_id, ext)
        result   = _blob_put(key, raw, content_type=mime)
        blob_url = result.get("url", "")
    else:
        store = get_store(camara)
        d = os.path.join(store.data_repo_dir, group, commission_name, "docs")
        os.makedirs(d, exist_ok=True)
        safe_date   = sesion_fecha.replace("/", "-").replace(" ", "_")
        stored_name = f"{safe_date}_{doc_id}{ext}" if safe_date else f"{doc_id}{ext}"
        with open(os.path.join(d, stored_name), "wb") as fout:
            fout.write(raw)
        blob_url = f"/api/docs/{camara}/{group}/{commission_name}/file/{stored_name}"

    meta  = load_docs_meta(camara, group, commission_name)
    entry = {
        "id":            doc_id,
        "original_name": file.filename or f"{doc_id}{ext}",
        "title":         title or file.filename or f"{doc_id}{ext}",
        "tipo":          tipo,
        "sesion_fecha":  sesion_fecha,
        "scope":         scope,
        "source":        "manual",
        "url":           blob_url,
        "notas":         notas,
        "size_bytes":    len(raw),
        "uploaded_at":   datetime.utcnow().isoformat() + "Z",
    }
    meta.append(entry)
    save_docs_meta(camara, group, commission_name, meta)
    return {"success": True, "doc": entry}


@app.post("/api/docs/{camara}/{group}/{commission_name}/link")
def add_doc_link(
    camara: str, group: str, commission_name: str,
    payload: dict = Body(...),
):
    meta   = load_docs_meta(camara, group, commission_name)
    doc_id = str(uuid.uuid4())[:8]
    entry  = {
        "id":            doc_id,
        "original_name": payload.get("title", ""),
        "title":         payload.get("title", "Documento externo"),
        "tipo":          payload.get("tipo", "otro"),
        "sesion_fecha":  payload.get("sesion_fecha", ""),
        "scope":         payload.get("scope", "sesion"),
        "source":        payload.get("source", "manual"),
        "url":           payload.get("url", ""),
        "notas":         payload.get("notas", ""),
        "size_bytes":    0,
        "uploaded_at":   datetime.utcnow().isoformat() + "Z",
    }
    meta.append(entry)
    save_docs_meta(camara, group, commission_name, meta)
    return {"success": True, "doc": entry}


@app.delete("/api/docs/{camara}/{group}/{commission_name}/{doc_id}")
def delete_doc(camara: str, group: str, commission_name: str, doc_id: str):
    meta  = load_docs_meta(camara, group, commission_name)
    entry = next((d for d in meta if d["id"] == doc_id), None)
    if not entry:
        return {"success": False, "error": "Documento no encontrado"}
    if BLOB_AVAILABLE and BLOB_TOKEN and entry.get("url", "").startswith("https://"):
        try:
            vercel_blob.delete(entry["url"], token=BLOB_TOKEN)
        except Exception as e:
            print(f"[WARN] No se pudo eliminar de Blob: {e}")
    meta = [d for d in meta if d["id"] != doc_id]
    save_docs_meta(camara, group, commission_name, meta)
    return {"success": True}


@app.get("/api/docs/{camara}/{group}/{commission_name}/file/{filename}")
def serve_doc_file(camara: str, group: str, commission_name: str, filename: str):
    store = get_store(camara)
    d     = os.path.join(store.data_repo_dir, group, commission_name, "docs")
    path  = os.path.join(d, filename)
    if not os.path.isfile(path):
        return {"error": "Archivo no encontrado"}
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    return FileResponse(path, media_type=mime, filename=filename)


# ─────────────────────────────────────────
# Noticias
# ─────────────────────────────────────────

@app.get("/api/news")
def news(source: str = "diario_oficial", q: str = ""):
    items = store_camara.news_feed(source=source, q=q)
    return {"success": True, "items": items, "total": len(items)}


# ─────────────────────────────────────────
# KOM Profiles  ←  ÚNICA ruta GET + ÚNICA ruta POST
# ─────────────────────────────────────────

@app.get("/api/kom/{chamber}/{pid}")
def get_kom_profile(chamber: str, pid: str):
    try:
        profile = _load_kom(chamber, pid)
        return {
            "success":     True,
            "exists":      bool(profile.get("updated_at")),
            "profile":     profile,
            "blob_active": bool(BLOB_AVAILABLE and BLOB_TOKEN),
        }
    except Exception as e:
        import traceback
        return {"success": False, "error": str(e), "trace": traceback.format_exc()[-500:]}


@app.post("/api/kom/{chamber}/{pid}")
def save_kom_profile(chamber: str, pid: str, payload: dict = Body(...)):
    payload["id"]         = pid
    payload["chamber"]    = chamber
    payload["updated_at"] = datetime.utcnow().isoformat() + "Z"
    try:
        url = _save_kom(chamber, pid, payload)
        return {"success": True, "saved": True, "url": url,
                "blob_active": bool(BLOB_AVAILABLE and BLOB_TOKEN)}
    except Exception as e:
        import traceback
        return {"success": False, "error": str(e), "trace": traceback.format_exc()[-500:]}


# ─────────────────────────────────────────
# Upload (Chat RAG)
# ─────────────────────────────────────────

@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    try:
        raw      = await file.read()
        saved_as = store_camara.save_upload(file.filename, raw)
        return {"success": True, "saved_as": saved_as}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/api/upload-doc")
async def upload_document(file: UploadFile = File(...)):
    try:
        file_content = await file.read()
        from vercel_blob import put
        blob = put(
            f"sesiones/{uuid.uuid4()}_{file.filename}",
            file_content,
            {"access": "public"},
        )
        return {"success": True, "url": blob["url"]}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ─────────────────────────────────────────
# Chat
# ─────────────────────────────────────────

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


# ─────────────────────────────────────────
# Debug
# ─────────────────────────────────────────

@app.get("/api/test-debug")
def test_debug():
    result = {
        "config": {
            "DATA_REPO_DIR":   DATA_REPO_DIR,   "exists_camara": os.path.isdir(DATA_REPO_DIR),
            "SENADO_REPO_DIR": SENADO_REPO_DIR, "exists_senado": os.path.isdir(SENADO_REPO_DIR),
            "KOM_DIR":         KOM_DIR,
            "blob_token_set":  bool(BLOB_TOKEN),
            "blob_sdk":        BLOB_AVAILABLE,
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
    safe_root = os.path.abspath(PROJECT_DIR)
    abs_path  = os.path.abspath(path)
    if not abs_path.startswith(safe_root):
        return {"error": "Acceso denegado"}
    if not os.path.isfile(abs_path):
        return {"error": "Archivo no encontrado"}
    return FileResponse(abs_path)