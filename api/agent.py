"""
agent.py — LegislativeAgent v3
Agente RAG legislativo:
  - Detecta automáticamente Senado vs Cámara según el store recibido
  - LEE el contenido completo de los documentos (no devuelve rutas)
  - Accede a .txt, .csv, .json y PDFs remotos
  - Reintentos con backoff exponencial
"""
from __future__ import annotations

import io
import logging
import os
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Optional

import httpx
from google import genai
from google.genai import errors as genai_errors

try:
    import pypdf
    _PDF_AVAILABLE = True
except ImportError:
    _PDF_AVAILABLE = False

logger = logging.getLogger(__name__)

# ─── Constantes ───────────────────────────────────────────────
DEFAULT_MODEL      = "gemini-1.5-flash"
DEFAULT_TOP_K      = 5
MAX_SNIPPET_LEN    = 1_200
MAX_PDF_CHARS      = 3_000
MAX_RETRIES        = 3
RETRY_DELAY_SEC    = 2.0
PDF_FETCH_TIMEOUT  = 20

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


# ─── PDF helpers ──────────────────────────────────────────────
def _extract_pdf_text(pdf_bytes: bytes) -> str:
    if not _PDF_AVAILABLE:
        return ""
    try:
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        parts = []
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
            resp = client.get(url, headers={"User-Agent": "LexiaBot/3.0"})
            resp.raise_for_status()
            ct = resp.headers.get("content-type", "")
            if "pdf" not in ct and not url.lower().endswith(".pdf"):
                return ""
            return _extract_pdf_text(resp.content)
    except Exception as e:
        logger.warning("No se pudo obtener PDF %s: %s", url, e)
        return ""


# ─── Path helpers ─────────────────────────────────────────────
def _short_label(path: str, store) -> str:
    """Etiqueta legible — muestra nombre de comisión y archivo, NO la ruta completa."""
    for base in [store.data_repo_dir, store.kom_dir]:
        if base and os.path.abspath(path).startswith(os.path.abspath(base)):
            rel   = os.path.relpath(path, base).replace("\\", "/")
            parts = rel.split("/")
            # Devuelve "Comisión X / sesión Y" en vez de rutas técnicas
            return "/".join(parts[-3:]) if len(parts) > 3 else rel
    return os.path.basename(path)


def source_md_link(path: str, store) -> str:
    label = _short_label(path, store)
    url   = "/api/file?path=" + urllib.parse.quote(path)
    return f"- [{label}]({url})"


# ─── Agent ────────────────────────────────────────────────────
class LegislativeAgent:
    """
    Agente RAG que LEE documentos legislativos y genera resúmenes,
    detectando automáticamente la cámara según el store recibido.
    """

    def __init__(
        self,
        store,
        gemini_api_key: str,
        model: str = DEFAULT_MODEL,
        top_k: int  = DEFAULT_TOP_K,
    ) -> None:
        self.store  = store
        self.model  = model
        self.top_k  = top_k
        self.ready  = bool(gemini_api_key and gemini_api_key.strip())
        self.client = genai.Client(api_key=gemini_api_key.strip()) if self.ready else None
        # Detecta la cámara desde el store para incluirla en el prompt
        self.camara = getattr(store, "default_chamber", "camara")
        logger.info("LegislativeAgent v3 — cámara: %s | modelo: %s", self.camara, model)

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

        # 1. Recuperar documentos relevantes
        raw_hits = self._retrieve(question)

        # 2. Extraer texto de PDFs referenciados
        pdf_snippets, pdfs_fetched = self._collect_pdf_context(raw_hits)

        if not raw_hits and not pdf_snippets:
            camara_label = "el Senado" if self.camara == "senado" else "la Cámara de Diputados"
            return AgentResponse(
                answer=f"No encontré información relevante en los documentos de {camara_label} para esta consulta.",
                hits_found=0,
            )

        # 3. Construir contexto legible (contenido, no rutas)
        context_block, source_paths = self._build_context(raw_hits, pdf_snippets)

        # 4. Generar respuesta
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
        try:
            return self.store.search_texts(question, top_k=self.top_k) or []
        except Exception:
            logger.exception("Error en search_texts")
            return []

    def _collect_pdf_context(self, hits: list[dict]) -> tuple[list[dict], int]:
        pdf_snippets: list[dict] = []
        fetched = 0
        seen_urls: set[str] = set()

        url_fields = ("url", "citacion", "pdf_url", "Citacion", "Cuenta", "Acta")
        for h in hits:
            snippet_text = h.get("snippet", "")
            candidates: list[str] = []

            for f in url_fields:
                v = h.get(f, "")
                if v and isinstance(v, str) and v.startswith("http"):
                    candidates.append(v)

            found_urls = re.findall(r'https?://[^\s"\'<>]+\.pdf[^\s"\'<>]*', snippet_text, re.IGNORECASE)
            candidates.extend(found_urls)

            for url in candidates:
                if url in seen_urls or fetched >= 4:
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
        self._ref_tags: list[str] = []   # referencias clickeables para el frontend
        camara_label = "Senado" if self.camara == "senado" else "Cámara de Diputados"

        for i, h in enumerate(hits, 1):
            file_path = h.get("file", "")
            label     = _short_label(file_path, self.store)
            snippet   = (h.get("snippet") or "")[:MAX_SNIPPET_LEN].strip()
            sections.append(f"[{camara_label} · Doc {i}: {label}]\n{snippet}")
            source_paths.append(file_path)
            # Genera referencia clickeable para el frontend
            api_url = "/api/file?path=" + urllib.parse.quote(file_path)
            self._ref_tags.append(f"[[REF:{label}:{api_url}]]")

        for j, p in enumerate(pdf_snippets, 1):
            sections.append(f"[PDF {j}: {p.get('label','')}]\n{p.get('text','').strip()}")
            url = p.get("url", "")
            if url:
                source_paths.append(url)
                self._ref_tags.append(f"[[REF:PDF · {p.get('label','')}:{url}]]")

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
                text = (resp.text or "").strip()
                if not text:
                    return "El modelo no generó respuesta. Reformule la pregunta.", "empty_response"
                logger.info("Respuesta recibida (intento %d, %d chars)", attempt, len(text))
                return text, None
            except genai_errors.APIError as exc:
                status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
                logger.warning("APIError intento %d status=%s", attempt, status)
                if status in (400, 401, 403):
                    return self._api_error_message(exc, status), f"api_error_{status}"
                last_error = exc
            except Exception as exc:
                logger.exception("Error inesperado intento %d", attempt)
                last_error = exc
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SEC * (2 ** (attempt - 1)))

        return "El servicio de IA no está disponible. Intente más tarde.", "max_retries_exceeded"

    @staticmethod
    def _api_error_message(exc: Exception, status: Optional[int]) -> str:
        if status == 400:
            return "Solicitud inválida. Reformule la pregunta."
        if status in (401, 403):
            return "Error de autenticación. Verifique que GEMINI_API_KEY sea válida y esté activa en Vercel."
        return f"Error del servicio de IA (código {status}). Intente más tarde."