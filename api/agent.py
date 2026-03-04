"""
agent.py — LegislativeAgent v2
Agente RAG legislativo con acceso completo a:
  - Archivos locales: .txt, .csv, .json
  - PDFs remotos (descarga + extracción de texto vía pypdf)
  - PDFs locales
"""

from __future__ import annotations

import io
import logging
import os
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Optional

import httpx
from google import genai
from google.genai import errors as genai_errors

# PDF extraction — graceful fallback if not installed
try:
    import pypdf
    _PDF_AVAILABLE = True
except ImportError:
    _PDF_AVAILABLE = False

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────
DEFAULT_MODEL       = "gemini-2.5-flash"
DEFAULT_TOP_K       = 8          # más contexto que antes
MAX_SNIPPET_LEN     = 2_000
MAX_PDF_CHARS       = 8_000      # máx. caracteres extraídos por PDF
MAX_RETRIES         = 3
RETRY_DELAY_SEC     = 2.0
PDF_FETCH_TIMEOUT   = 20         # segundos

SYSTEM_PROMPT = """\
Eres LEXIA, un analista legislativo experto, neutral y riguroso.

REGLAS ESTRICTAS:
1. Responde ÚNICAMENTE con información presente en el CONTEXTO proporcionado.
2. Si el contexto no contiene evidencia suficiente, indícalo explícitamente: \
   "No encontré información en los documentos disponibles sobre este punto."
3. Cita siempre la FUENTE de cada afirmación: (Fuente: <nombre del documento>).
4. Usa lenguaje formal, claro y sin sesgos políticos.
5. Estructura la respuesta con secciones cuando sea pertinente.
6. Nunca inventes datos, fechas, nombres ni cifras.
7. Si un documento PDF fue consultado y contiene la respuesta, menciónalo explícitamente.
"""

# ─────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────
@dataclass
class SearchHit:
    file: str
    score: float
    snippet: str
    source_url: Optional[str] = None


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


# ─────────────────────────────────────────────────────────────
# PDF helpers
# ─────────────────────────────────────────────────────────────
def _extract_pdf_text(pdf_bytes: bytes, max_chars: int = MAX_PDF_CHARS) -> str:
    """Extrae texto de un PDF en memoria. Devuelve string vacío si falla."""
    if not _PDF_AVAILABLE:
        logger.warning("pypdf no instalado — no se puede extraer texto de PDFs.")
        return ""
    try:
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        parts: list[str] = []
        for page in reader.pages:
            text = page.extract_text() or ""
            parts.append(text)
            if sum(len(p) for p in parts) >= max_chars:
                break
        return "\n".join(parts)[:max_chars]
    except Exception as exc:
        logger.warning("Error extrayendo PDF: %s", exc)
        return ""


def _fetch_pdf_from_url(url: str) -> str:
    """
    Descarga un PDF desde una URL y extrae su texto.
    Devuelve el texto o string vacío si falla.
    """
    try:
        with httpx.Client(timeout=PDF_FETCH_TIMEOUT, follow_redirects=True) as client:
            headers = {"User-Agent": "LexiaBot/2.0 (Legislative Research)"}
            resp = client.get(url, headers=headers)
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "")
            if "pdf" not in content_type and not url.lower().endswith(".pdf"):
                logger.info("URL no parece PDF (content-type: %s): %s", content_type, url)
                return ""
            text = _extract_pdf_text(resp.content)
            logger.info("PDF extraído desde %s → %d chars", url, len(text))
            return text
    except Exception as exc:
        logger.warning("No se pudo obtener PDF desde %s: %s", url, exc)
        return ""


# ─────────────────────────────────────────────────────────────
# Local PDF reader
# ─────────────────────────────────────────────────────────────
def _read_local_pdf(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return _extract_pdf_text(f.read())
    except Exception as exc:
        logger.warning("Error leyendo PDF local %s: %s", path, exc)
        return ""


# ─────────────────────────────────────────────────────────────
# Path helpers
# ─────────────────────────────────────────────────────────────
def _short_label(path: str, store) -> str:
    for base in [store.data_repo_dir, store.kom_dir]:
        if base and os.path.abspath(path).startswith(os.path.abspath(base)):
            rel   = os.path.relpath(path, base).replace("\\", "/")
            parts = rel.split("/")
            return "/".join(parts[-3:]) if len(parts) > 3 else rel
    return os.path.basename(path)


def source_md_link(path: str, store) -> str:
    label = _short_label(path, store)
    url   = "/api/file?path=" + urllib.parse.quote(path)
    return f"- [{label}]({url})"


# ─────────────────────────────────────────────────────────────
# Document scanner — walks ALL supported file types in the repo
# ─────────────────────────────────────────────────────────────
SUPPORTED_EXTENSIONS = {".txt", ".csv", ".json", ".pdf"}


def _iter_all_docs(store) -> list[dict]:
    """
    Recorre recursivamente data_repo_dir y kom_dir recopilando
    TODOS los archivos con extensiones soportadas.
    Devuelve lista de dicts con 'path' y 'ext'.
    """
    results: list[dict] = []
    for base_dir in [store.data_repo_dir, store.kom_dir]:
        if not base_dir or not os.path.isdir(base_dir):
            continue
        for root, _, files in os.walk(base_dir):
            for fname in files:
                ext = os.path.splitext(fname)[1].lower()
                if ext in SUPPORTED_EXTENSIONS:
                    results.append({"path": os.path.join(root, fname), "ext": ext})
    return results


# ─────────────────────────────────────────────────────────────
# Agent
# ─────────────────────────────────────────────────────────────
class LegislativeAgent:
    """
    Agente RAG legislativo con acceso completo a repositorios.

    Capacidades:
    - Búsqueda en archivos .txt, .csv, .json del DataStore
    - Lectura de PDFs locales
    - Descarga y extracción de PDFs remotos referenciados en los datos
    - Reintentos automáticos con backoff exponencial
    """

    def __init__(
        self,
        store,
        gemini_api_key: str,
        model: str = DEFAULT_MODEL,
        top_k: int = DEFAULT_TOP_K,
    ) -> None:
        self.store  = store
        self.model  = model
        self.top_k  = top_k
        self.ready  = bool(gemini_api_key and gemini_api_key.strip())
        self.client = genai.Client(api_key=gemini_api_key.strip()) if self.ready else None

        if self.ready:
            logger.info("LegislativeAgent v2 listo — modelo: %s | top_k: %d", model, top_k)
        else:
            logger.warning("LegislativeAgent en modo degradado: GEMINI_API_KEY ausente.")

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
        if not self.ready:
            return AgentResponse(
                answer="⚠️ Agente no disponible: falta `GEMINI_API_KEY`.",
                error="no_api_key",
            )

        # 1. Recuperar hits de texto del DataStore
        raw_hits = self._retrieve(question)

        # 2. Buscar y extraer PDFs (locales + remotos) referenciados
        pdf_snippets, pdfs_fetched = self._collect_pdf_context(question, raw_hits)

        if not raw_hits and not pdf_snippets:
            return AgentResponse(
                answer="No encontré información relevante en los repositorios legislativos para esta consulta.",
                hits_found=0,
            )

        # 3. Construir contexto combinado
        context_block, source_paths = self._build_context(raw_hits, pdf_snippets)

        # 4. Generar respuesta
        prompt = self._build_prompt(question, context_block)
        answer, error = self._generate_with_retry(prompt)

        return AgentResponse(
            answer=answer,
            sources=source_paths,
            hits_found=len(raw_hits),
            pdfs_fetched=pdfs_fetched,
            error=error,
        )

    def _retrieve(self, question: str) -> list[dict]:
        """Recupera hits del DataStore (txt/csv/json)."""
        try:
            return self.store.search_texts(question, top_k=self.top_k) or []
        except Exception:
            logger.exception("Error en search_texts para: %r", question)
            return []

    def _collect_pdf_context(
        self, question: str, hits: list[dict]
    ) -> tuple[list[dict], int]:
        """
        1. Busca archivos .pdf locales en el repositorio que coincidan con la query.
        2. Busca URLs de PDF en los hits (campo 'url', 'citacion', 'pdf_url', etc.)
           y descarga su contenido.
        Devuelve (lista de snippets PDF, cantidad de PDFs procesados).
        """
        pdf_snippets: list[dict] = []
        fetched = 0
        seen_urls: set[str] = set()

        # ── A) PDFs locales ───────────────────────────────────
        try:
            all_docs = _iter_all_docs(self.store)
            local_pdfs = [d for d in all_docs if d["ext"] == ".pdf"]
            q_lower = question.lower().split()

            for doc in local_pdfs[:30]:  # límite de seguridad
                fname = os.path.basename(doc["path"]).lower()
                if any(w in fname for w in q_lower if len(w) > 3):
                    text = _read_local_pdf(doc["path"])
                    if text.strip():
                        pdf_snippets.append({
                            "label": _short_label(doc["path"], self.store),
                            "text":  text[:MAX_SNIPPET_LEN],
                            "url":   None,
                        })
                        fetched += 1
        except Exception:
            logger.exception("Error escaneando PDFs locales")

        # ── B) PDFs remotos desde hits ────────────────────────
        url_fields = ("url", "citacion", "pdf_url", "edicion_url", "link", "href")
        for h in hits:
            # Los hits pueden tener metadata anidada en el snippet JSON
            snippet_text = h.get("snippet", "")
            candidate_urls: list[str] = []

            # buscar URLs directas en campos conocidos
            for field_name in url_fields:
                val = h.get(field_name, "")
                if val and isinstance(val, str) and val.startswith("http"):
                    candidate_urls.append(val)

            # buscar URLs de PDF dentro del texto del snippet
            import re
            found = re.findall(r'https?://[^\s"\'<>]+\.pdf[^\s"\'<>]*', snippet_text, re.IGNORECASE)
            candidate_urls.extend(found)

            for url in candidate_urls:
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                text = _fetch_pdf_from_url(url)
                if text.strip():
                    pdf_snippets.append({
                        "label": url.split("/")[-1][:60] or "PDF remoto",
                        "text":  text[:MAX_SNIPPET_LEN],
                        "url":   url,
                    })
                    fetched += 1
                    if fetched >= 5:  # máx. 5 PDFs remotos por consulta
                        break
            if fetched >= 5:
                break

        return pdf_snippets, fetched

    def _build_context(
        self, hits: list[dict], pdf_snippets: list[dict]
    ) -> tuple[str, list[str]]:
        sections: list[str] = []
        source_paths: list[str] = []

        for i, h in enumerate(hits, 1):
            label   = _short_label(h.get("file", "?"), self.store)
            snippet = (h.get("snippet") or "")[:MAX_SNIPPET_LEN].strip()
            sections.append(f"[DOC-{i}] {label}\n{snippet}")
            source_paths.append(h.get("file", ""))

        for j, p in enumerate(pdf_snippets, 1):
            label = p.get("label", f"PDF-{j}")
            text  = p.get("text", "").strip()
            url   = p.get("url")
            header = f"[PDF-{j}] {label}" + (f" ({url})" if url else "")
            sections.append(f"{header}\n{text}")
            if url:
                source_paths.append(url)

        return "\n\n---\n\n".join(sections), source_paths

    def _build_prompt(self, question: str, context: str) -> str:
        return (
            f"{SYSTEM_PROMPT}\n\n"
            f"PREGUNTA:\n{question}\n\n"
            f"CONTEXTO DOCUMENTAL:\n{context}\n\n"
            f"RESPUESTA ANALÍTICA:"
        )

    def _generate_with_retry(self, prompt: str) -> tuple[str, Optional[str]]:
        last_error: Optional[Exception] = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = self.client.models.generate_content(
                    model=self.model,
                    contents=prompt,
                )
                text = (resp.text or "").strip()
                if not text:
                    return "El modelo no generó una respuesta. Reformule la pregunta.", "empty_response"
                logger.info("Respuesta Gemini recibida (intento %d, %d chars)", attempt, len(text))
                return text, None

            except genai_errors.APIError as exc:
                status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
                logger.warning("APIError Gemini intento %d/%d status=%s", attempt, MAX_RETRIES, status)
                if status in (400, 401, 403):
                    return self._api_error_message(exc, status), f"api_error_{status}"
                last_error = exc

            except Exception as exc:
                logger.exception("Error inesperado Gemini intento %d/%d", attempt, MAX_RETRIES)
                last_error = exc

            if attempt < MAX_RETRIES:
                delay = RETRY_DELAY_SEC * (2 ** (attempt - 1))
                logger.info("Reintentando en %.1fs…", delay)
                time.sleep(delay)

        logger.error("Gemini agotó reintentos. Último error: %s", last_error)
        return "El servicio de IA no está disponible. Intente más tarde.", "max_retries_exceeded"

    @staticmethod
    def _api_error_message(exc: Exception, status: Optional[int]) -> str:
        if status == 400:
            return "Solicitud inválida. Reformule la pregunta."
        if status in (401, 403):
            return "Error de autenticación. Verifique que GEMINI_API_KEY sea válida y esté activa."
        return f"Error del servicio de IA (código {status}). Intente más tarde."