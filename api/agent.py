"""
agent.py — LegislativeAgent v4 (RAG + Inventario)
- Detecta Senado vs Cámara según store.default_chamber
- RAG para preguntas de contenido (search_texts + Gemini)
- Inventario (listar comisiones / contar docs / verificar comisión) SIN Gemini:
    - usa store.list_commissions() si existe
    - si no existe, escanea filesystem en store.data_repo_dir / store.kom_dir (fallback tipo "glob")
- Lee PDFs remotos y agrega snippets
- Reintentos con backoff exponencial
"""

from __future__ import annotations

import io
import logging
import os
import re
import time
import urllib.parse
import unicodedata
from dataclasses import dataclass, field
from typing import Optional, Any, Dict, List, Tuple

import httpx
from google import genai
from google.genai import errors as genai_errors

try:
    # opcional: útil en local. En Vercel normalmente NO lo necesitas.
    from dotenv import load_dotenv  # type: ignore
    _DOTENV_AVAILABLE = True
except Exception:
    _DOTENV_AVAILABLE = False

try:
    import pypdf  # type: ignore
    _PDF_AVAILABLE = True
except ImportError:
    _PDF_AVAILABLE = False

logger = logging.getLogger(__name__)

# ─── Constantes ───────────────────────────────────────────────
DEFAULT_MODEL      = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
DEFAULT_TOP_K      = int(os.getenv("RAG_TOP_K", "8"))        # subido (antes 5)
DEFAULT_TOP_K_WIDE = int(os.getenv("RAG_TOP_K_WIDE", "30"))  # para preguntas amplias por comisión
MAX_SNIPPET_LEN    = 1_200
MAX_PDF_CHARS      = 3_000
MAX_RETRIES        = 3
RETRY_DELAY_SEC    = 2.0
PDF_FETCH_TIMEOUT  = 20
MAX_PDFS_FETCHED   = 4

SYSTEM_PROMPT = """\
Eres LEXIA, un analista legislativo experto, neutral y riguroso de Chile.

REGLAS ESTRICTAS:
1. Responde ÚNICAMENTE con información presente en el CONTEXTO proporcionado.
2. LEE y RESUME el contenido de los documentos — NO menciones rutas de archivos ni nombres técnicos de archivos.
3. Si el contexto no contiene información suficiente, dilo explícitamente.
4. Cita la fuente como el nombre de la comisión o tipo de documento, NO como ruta de archivo.
5. Usa lenguaje formal, claro y sin sesgos políticos.
6. Organiza la respuesta con párrafos claros. Usa listas cuando sea útil.
7. Nunca inventes datos, fechas, nombres ni cifras.
8. Si te preguntan por el Senado, responde SOLO con información del Senado.
   Si te preguntan por Diputados, responde SOLO con información de Diputados.
"""

# ─── Data classes ─────────────────────────────────────────────
@dataclass
class AgentResponse:
    answer: str
    sources: list[str] = field(default_factory=list)
    hits_found: int = 0
    pdfs_fetched: int = 0
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None


# ─── Normalización ────────────────────────────────────────────
def _norm(s: str) -> str:
    s = (s or "").strip().lower()
    s = "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
    s = re.sub(r"\s+", " ", s)
    return s

def _looks_like_list_commissions(q: str) -> bool:
    nq = _norm(q)
    triggers = (
        "que comisiones estan disponibles",
        "que comisiones hay",
        "listar comisiones",
        "lista comisiones",
        "lista de comisiones",
        "comisiones disponibles",
        "cuantas comisiones",
        "catalogo de comisiones",
    )
    if any(t in nq for t in triggers):
        return True
    # fallback: pregunta general + palabra comisiones
    if "comisiones" in nq and any(k in nq for k in ("dispon", "listar", "catalog", "cuantas", "todas")):
        return True
    return False

def _looks_like_commission_inventory(q: str) -> bool:
    """
    Preguntas tipo:
    - "tienes documentos de la comisión de salud?"
    - "hay actas/cuentas/transcripts de X?"
    - "qué documentos existen de X?"
    """
    nq = _norm(q)
    if "comision" not in nq:
        return False
    if any(k in nq for k in ("tienes", "hay", "existe", "document", "acta", "cuenta", "transcript", "video", "mis docs", "docs")):
        return True
    return False


# ─── PDF helpers ──────────────────────────────────────────────
def _extract_pdf_text(pdf_bytes: bytes) -> str:
    if not _PDF_AVAILABLE:
        return ""
    try:
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        parts: list[str] = []
        for page in reader.pages:
            parts.append(page.extract_text() or "")
            if sum(len(p) for p in parts) >= MAX_PDF_CHARS:
                break
        return "\n".join(parts)[:MAX_PDF_CHARS]
    except Exception as e:
        logger.warning("Error extrayendo PDF: %s", e)
        return ""

def _fetch_pdf_from_url(url: str) -> str:
    try:
        with httpx.Client(timeout=PDF_FETCH_TIMEOUT, follow_redirects=True) as client:
            resp = client.get(url, headers={"User-Agent": "LexiaBot/4.0"})
            resp.raise_for_status()
            ct = resp.headers.get("content-type", "")
            if "pdf" not in ct and not url.lower().endswith(".pdf"):
                return ""
            return _extract_pdf_text(resp.content)
    except Exception as e:
        logger.warning("No se pudo obtener PDF %s: %s", url, e)
        return ""


# ─── Labels / referencias ─────────────────────────────────────
def _short_label(path: str, store) -> str:
    """
    Etiqueta legible: intenta mostrar "Comisión / año / sesión" y NO el filename técnico.
    Quita extensiones para evitar que el modelo cite "historial.csv".
    """
    try:
        bases = [getattr(store, "data_repo_dir", None), getattr(store, "kom_dir", None)]
        for base in bases:
            if base and os.path.abspath(path).startswith(os.path.abspath(base)):
                rel = os.path.relpath(path, base).replace("\\", "/")
                parts = rel.split("/")
                tail = parts[-3:] if len(parts) >= 3 else parts
                if tail:
                    tail[-1] = os.path.splitext(tail[-1])[0]
                return " / ".join(tail)
    except Exception:
        pass
    return os.path.splitext(os.path.basename(path))[0]

def _ref_link(label: str, url: str) -> str:
    # Tag amigable para tu frontend
    return f"[[REF:{label}:{url}]]"


# ─── Inventario por filesystem (fallback) ──────────────────────
def _scan_commissions_from_fs(store) -> Dict[str, List[str]]:
    """
    Fallback si tu store no tiene list_commissions():
    Recorre store.data_repo_dir y store.kom_dir buscando carpetas/archivos.
    Devuelve dict: {"Permanentes":[...], "Especiales":[...], "Mixtas":[...], "Otras":[...]} (best-effort)
    """
    bases = []
    for attr in ("data_repo_dir", "kom_dir"):
        p = getattr(store, attr, None)
        if p and os.path.isdir(p):
            bases.append(p)

    groups: Dict[str, set[str]] = {
        "Permanentes": set(),
        "Especiales": set(),
        "Mixtas": set(),
        "Presupuesto": set(),
        "Unidas": set(),
        "Otras": set(),
    }

    def classify(path_parts: List[str]) -> str:
        txt = " ".join(path_parts).lower()
        if "perman" in txt:
            return "Permanentes"
        if "especial" in txt:
            return "Especiales"
        if "mixta" in txt:
            return "Mixtas"
        if "presup" in txt:
            return "Presupuesto"
        if "unid" in txt:
            return "Unidas"
        return "Otras"

    for base in bases:
        for root, dirs, files in os.walk(base):
            # Heurística: si hay archivos “sesión” dentro, tomamos el nombre de la carpeta como comisión
            # Comisión suele estar 1 nivel bajo "comisiones/..." o similar.
            parts = os.path.relpath(root, base).replace("\\", "/").split("/")
            if parts == ["."]:
                continue

            has_docs = any(f.lower().endswith((".txt", ".json", ".csv", ".pdf")) for f in files)
            if not has_docs:
                continue

            # nombre comisión: el último folder “semántico” (best effort)
            commission_name = parts[-1].strip()
            if not commission_name or commission_name.lower() in ("data", "docs", "sessions", "sesiones"):
                continue

            group = classify(parts)
            groups[group].add(commission_name)

    # normaliza salida
    out: Dict[str, List[str]] = {}
    for k, v in groups.items():
        if v:
            out[k] = sorted(v, key=lambda x: _norm(x))
    return out

def _commission_stats_from_fs(store, commission_query: str) -> Dict[str, Any]:
    """
    Cuenta docs por extensión para una comisión.
    Busca por match parcial del nombre de carpeta.
    """
    cq = _norm(commission_query)
    bases = []
    for attr in ("data_repo_dir", "kom_dir"):
        p = getattr(store, attr, None)
        if p and os.path.isdir(p):
            bases.append(p)

    counts = {"txt": 0, "json": 0, "csv": 0, "pdf": 0, "other": 0}
    matched_paths: List[str] = []

    for base in bases:
        for root, _, files in os.walk(base):
            rel_parts = os.path.relpath(root, base).replace("\\", "/").split("/")
            if rel_parts == ["."]:
                continue
            # match por carpeta
            if not any(cq in _norm(p) for p in rel_parts):
                continue

            for f in files:
                ext = os.path.splitext(f.lower())[1]
                if ext == ".txt":
                    counts["txt"] += 1
                elif ext == ".json":
                    counts["json"] += 1
                elif ext == ".csv":
                    counts["csv"] += 1
                elif ext == ".pdf":
                    counts["pdf"] += 1
                elif ext:
                    counts["other"] += 1

            # guardamos algunos ejemplos (máximo 5)
            if len(matched_paths) < 5 and files:
                matched_paths.append(root)

    return {"counts": counts, "examples": matched_paths}


def _format_commissions_list(data: Any, camara: str) -> str:
    camara_title = "Senado" if camara == "senado" else "Cámara de Diputados"
    if isinstance(data, dict):
        lines = [f"Comisiones disponibles ({camara_title}):"]
        for group, items in data.items():
            if not items:
                continue
            lines.append(f"\n{group}:")
            for name in items:
                lines.append(f"- {name}")
        return "\n".join(lines).strip()
    if isinstance(data, list):
        lines = [f"Comisiones disponibles ({camara_title}):"]
        for name in data:
            lines.append(f"- {name}")
        return "\n".join(lines).strip()
    return f"No pude listar comisiones ({camara_title}): índice no disponible."

def _format_commission_stats(stats: Dict[str, Any], camara: str, commission_name: str) -> str:
    camara_title = "Senado" if camara == "senado" else "Cámara de Diputados"
    c = stats.get("counts", {})
    total = sum(int(v) for v in c.values()) if isinstance(c, dict) else 0
    lines = [
        f"Inventario de documentos — {camara_title}",
        f"Comisión consultada (búsqueda): {commission_name}",
        f"- Total aproximado de archivos: {total}",
        f"- TXT: {c.get('txt',0)} | JSON: {c.get('json',0)} | CSV: {c.get('csv',0)} | PDF: {c.get('pdf',0)} | Otros: {c.get('other',0)}",
    ]
    examples = stats.get("examples") or []
    if examples:
        lines.append("\nEjemplos de carpetas donde se encontraron documentos (muestra):")
        for p in examples[:5]:
            lines.append(f"- {p}")
    return "\n".join(lines).strip()


# ─── Agent ────────────────────────────────────────────────────
class LegislativeAgent:
    """
    Agente RAG + Inventario:
    - inventario de comisiones y documentos (SIN Gemini)
    - preguntas de contenido: RAG + Gemini
    """

    def __init__(
        self,
        store,
        gemini_api_key: str,
        model: str = DEFAULT_MODEL,
        top_k: int = DEFAULT_TOP_K,
    ) -> None:
        # .env local opcional
        if _DOTENV_AVAILABLE and os.getenv("LOAD_DOTENV", "").strip() == "1":
            try:
                # busca .env en raíz del proyecto (estilo tu agente viejo)
                base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                load_dotenv(os.path.join(base_dir, ".env"))
            except Exception:
                pass

        self.store = store
        self.model = model
        self.top_k = top_k
        self.ready = bool(gemini_api_key and gemini_api_key.strip())
        self.client = genai.Client(api_key=gemini_api_key.strip()) if self.ready else None
        self.camara = getattr(store, "default_chamber", "camara")

        logger.info("LegislativeAgent v4 — cámara: %s | modelo: %s | top_k=%s", self.camara, self.model, self.top_k)

    # ── Interfaz pública ──────────────────────────────────────
    def ask(self, question: str) -> str:
        return self._ask_structured(question).answer

    def ask_structured(self, question: str) -> AgentResponse:
        return self._ask_structured(question)

    # ── Implementación ────────────────────────────────────────
    def _ask_structured(self, question: str) -> AgentResponse:
        question = (question or "").strip()
        if not question:
            return AgentResponse(answer="⚠️ La pregunta está vacía.", error="empty_question")

        # 0) Router de inventario: listar comisiones (NO usa IA)
        if _looks_like_list_commissions(question):
            data = None
            # preferir store si lo expone
            for fn_name in ("list_commissions", "get_commissions", "commissions_index"):
                fn = getattr(self.store, fn_name, None)
                if callable(fn):
                    try:
                        data = fn(chamber=self.camara)
                    except TypeError:
                        data = fn()
                    break
            # fallback filesystem
            if data is None:
                data = _scan_commissions_from_fs(self.store)

            return AgentResponse(answer=_format_commissions_list(data, self.camara), hits_found=0)

        # 1) Router de inventario: “¿tienes docs de la comisión X?”
        if _looks_like_commission_inventory(question):
            # extrae algo como "comision de salud"
            # si no se puede, usa la pregunta completa como query
            m = re.search(r"comision(?: de)?\s+(.+)$", _norm(question))
            commission_q = (m.group(1) if m else question).strip()
            stats = _commission_stats_from_fs(self.store, commission_q)
            return AgentResponse(answer=_format_commission_stats(stats, self.camara, commission_q), hits_found=0)

        # 2) Para preguntas de contenido, necesitamos IA
        if not self.ready:
            return AgentResponse(
                answer="⚠️ Agente no disponible: falta `GEMINI_API_KEY`.",
                error="no_api_key",
            )

        # 3) Recuperar documentos relevantes (RAG)
        raw_hits = self._retrieve(question)

        # 4) Extraer texto de PDFs referenciados
        pdf_snippets, pdfs_fetched = self._collect_pdf_context(raw_hits)

        if not raw_hits and not pdf_snippets:
            camara_label = "el Senado" if self.camara == "senado" else "la Cámara de Diputados"
            return AgentResponse(
                answer=f"No encontré información relevante en los documentos de {camara_label} para esta consulta.",
                hits_found=0,
            )

        # 5) Construir contexto legible (contenido, no rutas)
        context_block, source_paths = self._build_context(raw_hits, pdf_snippets)

        # 6) Generar respuesta
        prompt = self._build_prompt(question, context_block)
        answer, error = self._generate_with_retry(prompt)

        # Añade referencias clickeables al final de la respuesta
        refs = getattr(self, "_ref_tags", [])
        if refs and not error:
            answer = answer.rstrip() + "\n\n" + " ".join(refs)

        return AgentResponse(
            answer=answer,
            sources=source_paths,
            hits_found=len(raw_hits),
            pdfs_fetched=pdfs_fetched,
            error=error,
        )

    def _retrieve(self, question: str) -> list[dict]:
        """
        Mejora recall:
        - si pregunta por comisión, sube top_k
        - intenta filtrar por cámara si el path lo sugiere
        """
        try:
            qn = _norm(question)
            dyn_top_k = DEFAULT_TOP_K_WIDE if "comision" in qn else self.top_k

            hits = self.store.search_texts(question, top_k=dyn_top_k) or []

            cam = self.camara
            filtered: list[dict] = []
            for h in hits:
                fp = (h.get("file") or "").lower()
                if cam == "senado":
                    # si el path sugiere cámara, lo descartamos
                    if "diput" in fp or "camara" in fp:
                        continue
                else:
                    if "senado" in fp:
                        continue
                filtered.append(h)

            return filtered
        except Exception:
            logger.exception("Error en search_texts")
            return []

    def _collect_pdf_context(self, hits: list[dict]) -> tuple[list[dict], int]:
        pdf_snippets: list[dict] = []
        fetched = 0
        seen_urls: set[str] = set()

        url_fields = ("url", "citacion", "pdf_url", "Citacion", "Cuenta", "Acta")
        for h in hits:
            snippet_text = h.get("snippet", "") or ""
            candidates: list[str] = []

            for f in url_fields:
                v = h.get(f, "")
                if v and isinstance(v, str) and v.startswith("http"):
                    candidates.append(v)

            found_urls = re.findall(r'https?://[^\s"\'<>]+\.pdf[^\s"\'<>]*', snippet_text, re.IGNORECASE)
            candidates.extend(found_urls)

            for url in candidates:
                if url in seen_urls or fetched >= MAX_PDFS_FETCHED:
                    continue
                seen_urls.add(url)
                text = _fetch_pdf_from_url(url)
                if text.strip():
                    pdf_snippets.append({"label": url.split("/")[-1][:60], "text": text[:MAX_SNIPPET_LEN], "url": url})
                    fetched += 1

        return pdf_snippets, fetched

    def _build_context(self, hits: list[dict], pdf_snippets: list[dict]) -> tuple[str, list[str]]:
        sections: list[str] = []
        source_paths: list[str] = []
        self._ref_tags: list[str] = []

        camara_label = "Senado" if self.camara == "senado" else "Cámara de Diputados"

        for i, h in enumerate(hits, 1):
            file_path = h.get("file", "")
            label = _short_label(file_path, self.store)
            snippet = (h.get("snippet") or "")[:MAX_SNIPPET_LEN].strip()

            sections.append(f"[{camara_label} · Doc {i}: {label}]\n{snippet}")
            source_paths.append(file_path)

            api_url = "/api/file?path=" + urllib.parse.quote(file_path)
            self._ref_tags.append(_ref_link(label, api_url))

        for j, p in enumerate(pdf_snippets, 1):
            sections.append(f"[PDF {j}: {p.get('label','')}]\n{p.get('text','').strip()}")
            url = p.get("url", "")
            if url:
                source_paths.append(url)
                self._ref_tags.append(_ref_link(f"PDF · {p.get('label','')}", url))

        return "\n\n---\n\n".join(sections), source_paths

    def _build_prompt(self, question: str, context: str) -> str:
        camara_label = "el Senado de Chile" if self.camara == "senado" else "la Cámara de Diputados de Chile"
        return (
            f"{SYSTEM_PROMPT}\n\n"
            f"CÁMARA CONSULTADA: {camara_label}\n\n"
            f"PREGUNTA: {question}\n\n"
            f"CONTEXTO DOCUMENTAL ({camara_label}):\n{context}\n\n"
            f"RESPUESTA (resume el contenido con claridad — NO menciones rutas, nombres de archivo ni paths):"
        )

    def _generate_with_retry(self, prompt: str) -> tuple[str, Optional[str]]:
        last_error: Optional[Exception] = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = self.client.models.generate_content(model=self.model, contents=prompt)
                text = (getattr(resp, "text", "") or "").strip()
                if not text:
                    return "El modelo no generó respuesta. Reformule la pregunta.", "empty_response"
                logger.info("Respuesta recibida (intento %d, %d chars)", attempt, len(text))
                return text, None

            except genai_errors.APIError as exc:
                status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
                logger.warning("APIError intento %d status=%s detail=%r", attempt, status, str(exc))

                if status in (400, 401, 403):
                    return self._api_error_message(exc, status), f"api_error_{status}"
                if status == 429:
                    return "Límite de cuota/rate limit (429). Intente nuevamente en unos segundos.", "api_error_429"

                last_error = exc

            except Exception as exc:
                logger.exception("Error inesperado intento %d: %r", attempt, exc)
                last_error = exc

            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SEC * (2 ** (attempt - 1)))

        msg = f"El servicio de IA no está disponible. Último error: {type(last_error).__name__}: {last_error}"
        return msg, "max_retries_exceeded"

    @staticmethod
    def _api_error_message(exc: Exception, status: Optional[int]) -> str:
        if status == 400:
            return "Solicitud inválida. Reformule la pregunta."
        if status in (401, 403):
            return "Error de autenticación. Verifique que GEMINI_API_KEY sea válida y esté activa en Vercel."
        return f"Error del servicio de IA (código {status}). Intente más tarde."