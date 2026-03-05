"""
agent.py — LEXIA LegislativeAgent v5
======================================
Agente RAG legislativo para el Observatorio Político Chile.

Capacidades:
  · Detecta automáticamente Senado vs Cámara de Diputados
  · Soporte dual-store (REPO_SENADO + REPO_V40) con selección inteligente
  · Detección de comisión específica → carga TODO su contenido directamente
  · Búsqueda general por score normalizado (tolerante a tildes/mayúsculas)
  · Lectura de PDFs remotos (Citación, Cuenta, Acta)
  · Respuestas con referencias clickeables [[REF:label:url]]
  · Router de inventario sin IA (listar comisiones, contar docs)
  · Reintentos con backoff exponencial
  · Prompts estructurados para Gemini 2.5 Flash

Estructura de archivos esperada por comisión:
  REPO/
    Permanentes/
      Comisión de Salud/
        comision.json          ← metadata de la comisión
        integrantes.json       ← senadores/diputados miembros
        historial.csv          ← todas las sesiones (ID, Fecha, Estado, URLs)
        sesiones_detail/
          22338.json           ← detalle de sesión individual
          Trancripciones/      ← (typo del senado, también soportado)
            22338.txt          ← transcripción completa
        transcripts/           ← alternativa para Diputados
          12345.txt
"""

from __future__ import annotations

import csv
import glob
import io
import json
import logging
import os
import re
import time
import unicodedata
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import httpx
from google import genai
from google.genai import errors as genai_errors

try:
    import pypdf
    _PDF_OK = True
except ImportError:
    _PDF_OK = False

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════
# CONFIGURACIÓN
# ══════════════════════════════════════════════════════════════
MODEL           = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
TOP_K           = int(os.getenv("RAG_TOP_K",      "8"))
TOP_K_WIDE      = int(os.getenv("RAG_TOP_K_WIDE", "20"))
MAX_SNIPPET          = 3_500   # chars por documento genérico en el contexto
MAX_TRANSCRIPT_SNIPPET = 8_000  # chars por transcripción (fuente más rica)
MAX_HIST_SNIPPET     = 10_000  # chars para historial.csv (necesita más para no perder URLs)
MAX_PDF_CHARS        = 6_000   # chars extraídos de un documento remoto (PDF o HTML)
MAX_PDFS             = 5       # documentos remotos máximo por consulta
MAX_TRANSCRIPTS      = 12      # transcripts locales máximo por comisión (antes: 8)
MAX_JSONS            = 8       # JSONs de sesión máximo por comisión (antes: 6)
PDF_TIMEOUT          = 25      # segundos
MAX_RETRIES          = 3
RETRY_BASE           = 2.0     # segundos base para backoff

# Dominios legislativos conocidos — se descargan aunque no sean .pdf
LEGISLATIVE_DOMAINS = (
    "camara.cl",
    "senado.cl",
    "bcn.cl",
    "leychile.cl",
    "diariooficial.interior.gob.cl",
)

# Carpetas donde pueden estar las transcripciones
TRANSCRIPT_DIRS = [
    "transcripts",
    "Trancripciones",   # typo del scraper del Senado
    "Transcripciones",
    "transcripciones",
]

SYSTEM_PROMPT = """\
Eres LEXIA, el asistente de análisis legislativo del Observatorio Político Chile.
Eres experto, neutral y riguroso. Tu función es ayudar a entender el trabajo
legislativo del Senado y la Cámara de Diputados de Chile.

REGLAS ABSOLUTAS:
1. Responde SOLO con información presente en el CONTEXTO proporcionado.
2. Nunca inventes datos, fechas, nombres, cifras ni votaciones.
3. NO menciones nombres de archivos, rutas ni extensiones (.csv, .json, .txt).
4. Cita la fuente como el nombre de la comisión o tipo de documento
   (ej: "historial de sesiones", "transcripción", "nómina de integrantes").
5. Sé claro, formal y sin sesgos políticos.
6. Organiza con párrafos y listas cuando sea útil.
7. Si el contexto no tiene suficiente información, dilo directamente.
8. Si la pregunta es sobre el Senado, responde SOLO con datos del Senado.
   Si es sobre Diputados, responde SOLO con datos de Diputados.
9. Si hay transcripciones disponibles, úsalas como fuente principal —
   contienen el debate real y son el documento más valioso.

INSTRUCCIONES DE CALIDAD:
- Si la pregunta pide un resumen o información sobre una sesión, debes ser
  EXHAUSTIVO: incluye todos los temas tratados, los participantes que intervinieron,
  los acuerdos alcanzados, las votaciones si las hubo, y cualquier detalle relevante.
  Un buen resumen ejecutivo tiene al menos 300-500 palabras.
- Si la pregunta menciona una fecha específica (ej: "3 de marzo", "15 de enero"),
  enfoca tu respuesta EXCLUSIVAMENTE en la sesión o documento de esa fecha.
  Si no hay información de esa fecha exacta en el contexto, dilo claramente.
- Si la pregunta pide la "última sesión" o "sesión más reciente", identifica
  la sesión con la fecha más reciente en el contexto y enfócate en ella.
- Cuando respondas sobre una sesión específica, siempre indica: fecha, número
  de sesión si está disponible, temas tratados, y conclusiones o acuerdos.
"""


# ══════════════════════════════════════════════════════════════
# DATACLASSES
# ══════════════════════════════════════════════════════════════
@dataclass
class AgentResponse:
    answer:       str
    sources:      List[str] = field(default_factory=list)
    hits_found:   int = 0
    pdfs_fetched: int = 0
    chamber_used: str = ""
    error:        Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class DocHit:
    file:    str
    score:   int
    snippet: str
    label:   str = ""


# ══════════════════════════════════════════════════════════════
# UTILIDADES DE TEXTO
# ══════════════════════════════════════════════════════════════
def _norm(s: str) -> str:
    """Normaliza texto: minúsculas, sin tildes, sin espacios extras."""
    s = (s or "").strip().lower()
    s = "".join(c for c in unicodedata.normalize("NFD", s)
                if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s)


def _score_query(query: str, text: str) -> int:
    """Score de relevancia tolerante a tildes. Bonus si aparece frase completa."""
    words  = [_norm(w) for w in query.split() if len(w) >= 2]
    text_n = _norm(text)
    if not words:
        return 0
    score = sum(text_n.count(w) for w in words)
    if len(words) > 1 and _norm(query) in text_n:
        score += 15
    return score


# Meses en español para parseo de fechas en lenguaje natural
_MESES = {
    "enero": "01", "febrero": "02", "marzo": "03", "abril": "04",
    "mayo": "05", "junio": "06", "julio": "07", "agosto": "08",
    "septiembre": "09", "octubre": "10", "noviembre": "11", "diciembre": "12",
}

def _extract_date_from_query(question: str) -> Optional[str]:
    """
    Intenta extraer una fecha mencionada en la pregunta.
    Soporta:
      · "3 de marzo", "15 de enero de 2025"
      · "2025-03-03", "03-03-2025", "03/03/2025"
    Retorna string en formato YYYY-MM-DD o None.
    """
    q = question.lower().strip()

    # Formato numérico: YYYY-MM-DD
    m = re.search(r'\b(20\d{2})[/-](\d{1,2})[/-](\d{1,2})\b', q)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"

    # Formato numérico: DD-MM-YYYY o DD/MM/YYYY
    m = re.search(r'\b(\d{1,2})[/-](\d{1,2})[/-](20\d{2})\b', q)
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"

    # Formato natural: "3 de marzo [de 2025]"
    pattern = (
        r'\b(\d{1,2})\s+de\s+('
        + "|".join(_MESES.keys())
        + r')(?:\s+de\s+(20\d{2}))?\b'
    )
    m = re.search(pattern, q)
    if m:
        day   = int(m.group(1))
        month = _MESES[m.group(2)]
        year  = m.group(3) or str(datetime.now().year)
        return f"{year}-{month}-{day:02d}"

    return None


def _date_matches_text(date_str: str, text: str) -> bool:
    """True si la fecha aparece en el texto (varios formatos)."""
    try:
        y, mo, d = date_str.split("-")
        patterns = [
            date_str,                        # 2025-03-03
            f"{d}-{mo}-{y}",                 # 03-03-2025
            f"{d}/{mo}/{y}",                 # 03/03/2025
            f"{int(d)} de {[k for k,v in _MESES.items() if v == mo][0]}",  # 3 de marzo
        ]
        text_l = text.lower()
        return any(p in text_l for p in patterns)
    except Exception:
        return False


# ══════════════════════════════════════════════════════════════
# LECTORES DE ARCHIVOS
# ══════════════════════════════════════════════════════════════
def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except Exception:
        return ""


def _read_json(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return json.load(f)
    except Exception:
        return None


def _read_csv(path: str) -> List[dict]:
    try:
        with open(path, "r", encoding="utf-8-sig", errors="ignore", newline="") as f:
            reader = csv.DictReader(f)
            rows = []
            for row in reader:
                clean = {
                    str(k).replace("\ufeff", "").strip():
                    (v.strip() if isinstance(v, str) else v)
                    for k, v in (row or {}).items() if k
                }
                rows.append(clean)
            return rows
    except Exception:
        return []


# ══════════════════════════════════════════════════════════════
# PDF REMOTO
# ══════════════════════════════════════════════════════════════
def _extract_pdf(pdf_bytes: bytes) -> str:
    if not _PDF_OK:
        return ""
    try:
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        parts, total = [], 0
        for page in reader.pages:
            t = page.extract_text() or ""
            parts.append(t)
            total += len(t)
            if total >= MAX_PDF_CHARS:
                break
        return "\n".join(parts)[:MAX_PDF_CHARS]
    except Exception as e:
        logger.warning("PDF extract error: %s", e)
        return ""


def _is_legislative_url(url: str) -> bool:
    """True si la URL pertenece a un dominio legislativo conocido."""
    try:
        host = urllib.parse.urlparse(url).netloc.lower()
        return any(d in host for d in LEGISLATIVE_DOMAINS)
    except Exception:
        return False


def _html_to_text(html: str) -> str:
    """Extrae texto limpio de HTML de forma simple (sin dependencias externas)."""
    # Eliminar scripts y estilos completos
    html = re.sub(r"<(script|style)[^>]*>.*?</(script|style)>", " ", html,
                  flags=re.IGNORECASE | re.DOTALL)
    # Reemplazar tags de bloque con salto de línea
    html = re.sub(r"<(br|p|div|li|tr|h[1-6])[^>]*>", "\n", html, flags=re.IGNORECASE)
    # Eliminar el resto de tags
    html = re.sub(r"<[^>]+>", " ", html)
    # Decodificar entidades HTML básicas
    html = html.replace("&nbsp;", " ").replace("&amp;", "&") \
               .replace("&lt;", "<").replace("&gt;", ">") \
               .replace("&quot;", '"').replace("&#39;", "'")
    # Colapsar espacios/saltos
    html = re.sub(r"[ \t]+", " ", html)
    html = re.sub(r"\n{3,}", "\n\n", html)
    return html.strip()


def _fetch_pdf(url: str) -> str:
    """
    Descarga una URL y extrae su contenido textual.
    Soporta:
      · PDFs (por Content-Type o extensión .pdf)
      · HTML de dominios legislativos conocidos (camara.cl, senado.cl, bcn.cl…)
    Retorna el texto extraído (vacío si falla o no es procesable).
    """
    try:
        with httpx.Client(timeout=PDF_TIMEOUT, follow_redirects=True) as client:
            r = client.get(url, headers={"User-Agent": "LEXIA/5.0"})
            r.raise_for_status()
            ct = r.headers.get("content-type", "").lower()

            # ── Caso 1: es un PDF ──────────────────────────────
            if "pdf" in ct or url.lower().endswith(".pdf"):
                return _extract_pdf(r.content)

            # ── Caso 2: HTML de dominio legislativo ───────────
            if ("html" in ct or "text" in ct) and _is_legislative_url(url):
                text = _html_to_text(r.text)
                return text[:MAX_PDF_CHARS]

            # ── Caso 3: content-type desconocido pero es .doc/.docx (descartado) ──
            logger.debug("URL ignorada (no PDF ni HTML legislativo): %s [ct=%s]", url, ct)
            return ""

    except Exception as e:
        logger.warning("_fetch_pdf failed %s: %s", url, e)
        return ""


# ══════════════════════════════════════════════════════════════
# ETIQUETAS LEGIBLES
# ══════════════════════════════════════════════════════════════
def _human_label(path: str, repo_dir: str, commission_name: str = "") -> str:
    """Convierte una ruta en etiqueta legible para el usuario."""
    base = os.path.basename(path)
    name, ext = os.path.splitext(base)
    ext = ext.lower()
    parent = _norm(os.path.basename(os.path.dirname(path)))

    if ext == ".txt" or any(_norm(d) in parent for d in TRANSCRIPT_DIRS):
        doc_type = f"Transcripción sesión {name}"
    elif ext == ".json" and name.isdigit():
        doc_type = f"Detalle sesión {name}"
    elif "historial" in name.lower():
        doc_type = "Historial de sesiones"
    elif "integrante" in name.lower():
        doc_type = "Nómina de integrantes"
    elif "comision" in name.lower():
        doc_type = "Información de la comisión"
    else:
        doc_type = name.replace("_", " ").title()

    return f"{commission_name} · {doc_type}" if commission_name else doc_type


def _ref_tag(label: str, path: str) -> str:
    url = "/api/file?path=" + urllib.parse.quote(path)
    return f"[[REF:{label}:{url}]]"


# ══════════════════════════════════════════════════════════════
# DETECCIÓN DE INTENCIÓN
# ══════════════════════════════════════════════════════════════
def _intent_list_commissions(q: str) -> bool:
    nq = _norm(q)
    phrases = [
        "que comisiones hay", "que comisiones estan", "comisiones disponibles",
        "listar comisiones", "lista de comisiones", "cuantas comisiones",
        "todas las comisiones", "catalogo de comisiones", "mostrar comisiones",
    ]
    if any(p in nq for p in phrases):
        return True
    return "comisiones" in nq and any(k in nq for k in ("todas", "cuantas", "listar", "disponib"))


def _intent_commission_inventory(q: str) -> bool:
    nq = _norm(q)
    if "comision" not in nq:
        return False
    return any(k in nq for k in (
        "tienes", "hay", "existe", "cuantos", "documentos",
        "actas", "transcripciones", "transcripts", "sesiones",
    ))


# ══════════════════════════════════════════════════════════════
# AGENTE PRINCIPAL
# ══════════════════════════════════════════════════════════════
class LegislativeAgent:
    """
    LEXIA — Agente RAG legislativo dual-cámara.

    Args:
        store:          DataStore principal (Senado o Cámara según contexto)
        gemini_api_key: Clave Gemini desde Vercel env vars
        store_alt:      DataStore de la otra cámara (opcional)
        model:          Modelo Gemini a usar
        top_k:          Número de documentos a recuperar
    """

    def __init__(
        self,
        store,
        gemini_api_key: str,
        store_alt=None,
        model: str = MODEL,
        top_k: int  = TOP_K,
    ) -> None:
        self.store     = store
        self.store_alt = store_alt
        self.model     = model
        self.top_k     = top_k
        self.camara    = getattr(store, "default_chamber", "camara")
        self.ready     = bool(gemini_api_key and gemini_api_key.strip())
        self.client    = genai.Client(api_key=gemini_api_key.strip()) if self.ready else None

        logger.info(
            "LEXIA v5 | camara=%s | model=%s | top_k=%d | dual=%s",
            self.camara, self.model, self.top_k, store_alt is not None,
        )

    # ─────────────────────────────────────────────────────────
    # INTERFAZ PÚBLICA
    # ─────────────────────────────────────────────────────────
    def ask(self, question: str) -> str:
        return self.ask_structured(question).answer

    def ask_structured(self, question: str) -> AgentResponse:
        question = (question or "").strip()
        if not question:
            return AgentResponse(answer="⚠️ La pregunta está vacía.", error="empty_question")

        # ── Router sin IA ────────────────────────────────────
        if _intent_list_commissions(question):
            return self._handle_list_commissions()

        if _intent_commission_inventory(question):
            return self._handle_commission_inventory(question)

        # ── RAG + Gemini ─────────────────────────────────────
        if not self.ready:
            return AgentResponse(
                answer="⚠️ Agente no disponible: falta GEMINI_API_KEY en las variables de entorno.",
                error="no_api_key",
            )

        store, chamber = self._select_store(question)
        hits           = self._retrieve(question, store)
        pdf_hits, n_pdfs = self._fetch_remote_pdfs(hits)

        if not hits and not pdf_hits:
            cam_label = "el Senado" if chamber == "senado" else "la Cámara de Diputados"
            return AgentResponse(
                answer=(
                    f"No encontré información relevante en los documentos de {cam_label} "
                    f"para esta consulta. Si buscas una comisión específica, "
                    f"menciona su nombre completo."
                ),
                chamber_used=chamber,
            )

        context, sources, ref_tags = self._build_context(hits, pdf_hits, chamber)
        prompt = self._build_prompt(question, context, chamber)
        answer, error = self._generate(prompt)

        if ref_tags and not error:
            answer = answer.rstrip() + "\n\n" + " ".join(ref_tags)

        return AgentResponse(
            answer=answer,
            sources=sources,
            hits_found=len(hits),
            pdfs_fetched=n_pdfs,
            chamber_used=chamber,
            error=error,
        )

    # ─────────────────────────────────────────────────────────
    # SELECCIÓN DE STORE / CÁMARA
    # ─────────────────────────────────────────────────────────
    def _detect_chamber(self, question: str) -> Optional[str]:
        q = _norm(question)
        if any(w in q for w in ["senado", "senador", "senadora", "senadores"]):
            return "senado"
        if any(w in q for w in ["diputado", "diputados", "diputada", "camara de diputados"]):
            return "camara"
        return None

    def _select_store(self, question: str) -> Tuple[Any, str]:
        """Retorna (store, chamber_name) más apropiado para la pregunta."""
        detected = self._detect_chamber(question)
        if detected == "senado":
            s = self.store if self.camara == "senado" else (self.store_alt or self.store)
            return s, "senado"
        if detected == "camara":
            s = self.store if self.camara == "camara" else (self.store_alt or self.store)
            return s, "camara"
        return self.store, self.camara

    # ─────────────────────────────────────────────────────────
    # RECUPERACIÓN DE DOCUMENTOS
    # ─────────────────────────────────────────────────────────
    def _retrieve(self, question: str, store) -> List[DocHit]:
        """
        Estrategia 1: comisión específica detectada → carga su contenido,
                      filtrando por fecha si la pregunta la menciona.
        Estrategia 2: búsqueda general por score en todo el repositorio.
        """
        target_date  = _extract_date_from_query(question)
        ask_latest   = any(w in _norm(question) for w in (
            "ultima", "último", "última", "reciente", "mas reciente",
            "última sesion", "sesion anterior", "anterior",
        ))

        commission = self._find_commission(store, question)
        if commission:
            group, name = commission
            hits = self._load_commission_docs(
                store, group, name,
                target_date=target_date,
                ask_latest=ask_latest,
            )
            if hits:
                logger.info(
                    "Comisión detectada: %s/%s → %d docs (fecha=%s, latest=%s)",
                    group, name, len(hits), target_date, ask_latest,
                )
                # Devolver MÁS docs cuando hay fecha o piden última sesión
                limit = self.top_k + 4 if not (target_date or ask_latest) else 20
                return hits[:limit]

        # Búsqueda general
        qn    = _norm(question)
        dyn_k = TOP_K_WIDE if "comision" in qn else self.top_k
        hits  = self._keyword_search(store, question, dyn_k)

        # Si no se especificó cámara y hay store_alt, combina
        if self.store_alt and self._detect_chamber(question) is None:
            alt = self._keyword_search(self.store_alt, question, dyn_k)
            hits = sorted(hits + alt, key=lambda h: h.score, reverse=True)

        return hits[:dyn_k]

    def _find_commission(self, store, question: str) -> Optional[Tuple[str, str]]:
        """Encuentra la comisión que mejor matchea la pregunta."""
        qn   = _norm(question)
        repo = getattr(store, "data_repo_dir", None)
        if not repo:
            return None

        best: Optional[Tuple[str, str]] = None
        best_score = 0

        for group in ("Permanentes", "Otras", "Unidas"):
            gdir = os.path.join(repo, group)
            if not os.path.isdir(gdir):
                continue
            for name in os.listdir(gdir):
                if not os.path.isdir(os.path.join(gdir, name)):
                    continue
                words = [w for w in _norm(name).split() if len(w) >= 4]
                if not words:
                    continue
                matched = sum(1 for w in words if w in qn)
                if matched > 0 and (matched / len(words)) >= 0.4 and matched > best_score:
                    best_score = matched
                    best = (group, name)

        return best

    def _load_commission_docs(
        self,
        store,
        group: str,
        name:  str,
        target_date: Optional[str] = None,
        ask_latest:  bool = False,
    ) -> List[DocHit]:
        """
        Carga documentos de una comisión.

        Mejoras respecto a la versión anterior:
        · Si target_date está presente, prioriza transcripts y JSONs de esa fecha.
        · Si ask_latest=True, ordena transcripts/JSONs por nombre desc (ID mayor = más reciente).
        · Snippets de transcripts ampliados a MAX_TRANSCRIPT_SNIPPET (8k chars).
        · Siempre incluye historial completo para que Gemini tenga la línea temporal.
        """
        repo = getattr(store, "data_repo_dir", "")
        base = os.path.join(repo, group, name)
        if not os.path.isdir(base):
            return []

        hits:          List[DocHit] = []
        priority_hits: List[DocHit] = []  # Hits que coinciden con la fecha buscada

        # ── 1. Recolectar TODOS los transcripts disponibles ────────────────
        all_txts: List[str] = []
        for td_name in TRANSCRIPT_DIRS:
            for td in [os.path.join(base, td_name),
                       os.path.join(base, "sesiones_detail", td_name)]:
                if os.path.isdir(td):
                    all_txts.extend(glob.glob(os.path.join(td, "*.txt")))

        # Ordenar: nombre desc (ID mayor = sesión más reciente)
        all_txts = sorted(set(all_txts), reverse=True)

        for p in all_txts[:MAX_TRANSCRIPTS]:
            text = _read_text(p)
            if not text.strip():
                continue
            doc = DocHit(
                file=p, score=300,
                snippet=text[:MAX_TRANSCRIPT_SNIPPET],
                label=_human_label(p, repo, name),
            )
            # Si hay fecha buscada y este transcript la menciona → prioridad máxima
            if target_date and _date_matches_text(target_date, text):
                doc.score = 1000
                priority_hits.append(doc)
            else:
                hits.append(doc)

        # ── 2. sesiones_detail JSONs ───────────────────────────────────────
        sd = os.path.join(base, "sesiones_detail")
        if os.path.isdir(sd):
            all_jsons = sorted(glob.glob(os.path.join(sd, "*.json")), reverse=True)
            for p in all_jsons[:MAX_JSONS]:
                obj = _read_json(p)
                if not obj:
                    continue
                text = json.dumps(obj, ensure_ascii=False)
                doc = DocHit(
                    file=p, score=200,
                    snippet=text[:MAX_SNIPPET],
                    label=_human_label(p, repo, name),
                )
                if target_date and _date_matches_text(target_date, text):
                    doc.score = 900
                    priority_hits.append(doc)
                else:
                    hits.append(doc)

        # ── 3. historial.csv completo (siempre incluido) ───────────────────
        hist = os.path.join(base, "historial.csv")
        if os.path.exists(hist):
            rows = _read_csv(hist)
            if rows:
                # Si hay fecha buscada, filtrar solo filas de esa fecha para el snippet
                if target_date:
                    matching = [r for r in rows if _date_matches_text(target_date, json.dumps(r))]
                    snippet_rows = matching if matching else rows
                else:
                    snippet_rows = rows
                hits.append(DocHit(
                    file=hist, score=150,
                    snippet=json.dumps(snippet_rows, ensure_ascii=False)[:MAX_HIST_SNIPPET],
                    label=_human_label(hist, repo, name),
                ))

        # ── 4. integrantes.json ────────────────────────────────────────────
        integ = os.path.join(base, "integrantes.json")
        if os.path.exists(integ):
            obj = _read_json(integ)
            if obj:
                hits.append(DocHit(
                    file=integ, score=120,
                    snippet=json.dumps(obj, ensure_ascii=False)[:MAX_SNIPPET],
                    label=_human_label(integ, repo, name),
                ))

        # ── 5. comision.json ───────────────────────────────────────────────
        cj = os.path.join(base, "comision.json")
        if os.path.exists(cj):
            obj = _read_json(cj)
            if obj:
                hits.append(DocHit(
                    file=cj, score=100,
                    snippet=json.dumps(obj, ensure_ascii=False)[:500],
                    label=_human_label(cj, repo, name),
                ))

        # ── Combinar: prioridad primero, luego el resto ────────────────────
        # Si pedimos "última sesión" y no hay fecha específica, el orden desc
        # ya garantiza que los primeros hits son los más recientes.
        all_hits = priority_hits + hits
        all_hits.sort(key=lambda h: h.score, reverse=True)
        return all_hits

    def _keyword_search(self, store, question: str, top_k: int) -> List[DocHit]:
        """Búsqueda por score de palabras clave usando el datastore."""
        try:
            repo = getattr(store, "data_repo_dir", "")
            raw  = store.search_texts(question, top_k=top_k) or []
            hits = []
            for r in raw:
                p    = r.get("file", "")
                comm = self._commission_from_path(p, repo)
                hits.append(DocHit(
                    file=p,
                    score=r.get("score", 0),
                    snippet=(r.get("snippet") or "")[:MAX_SNIPPET],
                    label=_human_label(p, repo, comm),
                ))
            return hits
        except Exception:
            logger.exception("Error en keyword_search")
            return []

    def _commission_from_path(self, path: str, repo: str) -> str:
        """Extrae nombre de comisión desde la ruta."""
        try:
            rel   = os.path.relpath(path, repo).replace("\\", "/")
            parts = rel.split("/")
            return parts[1] if len(parts) >= 2 else ""
        except Exception:
            return ""

    # ─────────────────────────────────────────────────────────
    # PDFs REMOTOS
    # ─────────────────────────────────────────────────────────
    def _fetch_remote_pdfs(self, hits: List[DocHit]) -> Tuple[List[dict], int]:
        """
        Descarga documentos referenciados en los hits.
        Soporta: PDFs + páginas HTML de dominios legislativos.
        Busca URLs en:
          · Campos clave del JSON (Citacion, Cuenta, Acta, Votacion, Video…)
          · Regex sobre el texto completo del snippet
        """
        pdf_hits: List[dict] = []
        fetched = 0
        seen: set[str] = set()

        # Columnas donde pueden vivir URLs en historial.csv o sesiones_detail JSON
        url_keys = (
            # Senado
            "Citacion", "Cuenta", "Acta", "citacion", "cuenta", "acta",
            # Camara de Diputados
            "Votacion", "votacion", "UrlCitacion", "UrlCuenta", "UrlActa",
            "urlCitacion", "urlCuenta", "urlActa",
            # Genéricos
            "url", "pdf_url", "URL", "Link", "link", "Enlace", "enlace",
            "documento", "Documento",
        )

        for h in hits:
            candidates: List[str] = []

            # ── Extraer URLs de campos JSON ────────────────────
            try:
                obj = json.loads(h.snippet)
                items = (
                    [obj] if isinstance(obj, dict)
                    else (obj[:50] if isinstance(obj, list) else [])
                )
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    for k in url_keys:
                        v = item.get(k, "")
                        if isinstance(v, str) and v.startswith("http"):
                            candidates.append(v)
            except Exception:
                pass

            # ── Regex: URLs de dominios legislativos (no solo .pdf) ──
            candidates += re.findall(
                r'https?://(?:www\.)?(?:camara|senado|bcn|leychile|diariooficial\.interior\.gob)'
                r'[^\s"\'<>]{3,}',
                h.snippet, re.IGNORECASE,
            )
            # Regex de respaldo: cualquier .pdf en el snippet
            candidates += re.findall(
                r'https?://[^\s"\'<>]+\.pdf[^\s"\'<>]*',
                h.snippet, re.IGNORECASE,
            )

            for url in candidates:
                if fetched >= MAX_PDFS or url in seen:
                    continue
                seen.add(url)
                text = _fetch_pdf(url)
                if text.strip():
                    label = url.split("/")[-1][:60] or url[:60]
                    pdf_hits.append({
                        "label": label,
                        "text":  text[:MAX_SNIPPET],
                        "url":   url,
                    })
                    fetched += 1

        return pdf_hits, fetched

    # ─────────────────────────────────────────────────────────
    # CONSTRUCCIÓN DE CONTEXTO Y PROMPT
    # ─────────────────────────────────────────────────────────
    def _build_context(
        self,
        hits: List[DocHit],
        pdf_hits: List[dict],
        chamber: str,
    ) -> Tuple[str, List[str], List[str]]:
        """Construye el bloque de contexto. Retorna (context, sources, ref_tags)."""
        sections:  List[str] = []
        sources:   List[str] = []
        ref_tags:  List[str] = []
        cam_label  = "Senado" if chamber == "senado" else "Cámara de Diputados"

        for i, h in enumerate(hits, 1):
            label = h.label or f"Documento {i}"
            sections.append(f"[{cam_label} · {label}]\n{h.snippet.strip()}")
            sources.append(h.file)
            ref_tags.append(_ref_tag(label, h.file))

        for p in pdf_hits:
            label = f"PDF · {p.get('label', '')}"
            sections.append(f"[{cam_label} · {label}]\n{p.get('text','').strip()}")
            url = p.get("url", "")
            if url:
                sources.append(url)
                ref_tags.append(f"[[REF:{label}:{url}]]")

        divider = "\n\n" + "─" * 60 + "\n\n"
        context = divider.join(sections)
        return context, sources, ref_tags

    def _build_prompt(self, question: str, context: str, chamber: str) -> str:
        cam_label   = "el Senado de Chile" if chamber == "senado" else "la Cámara de Diputados de Chile"
        target_date = _extract_date_from_query(question)
        bar = "═" * 60

        # Instrucción extra según el tipo de pregunta
        extra = ""
        if target_date:
            extra = (
                f"\n⚠️  INSTRUCCIÓN ESPECIAL: La pregunta menciona la fecha {target_date}. "
                f"Enfoca tu respuesta EXCLUSIVAMENTE en los documentos que correspondan "
                f"a esa fecha. Si hay transcripción de esa sesión, úsala como fuente "
                f"principal y extrae todos los detalles posibles: temas, participantes, "
                f"votaciones y conclusiones.\n"
            )
        elif any(w in _norm(question) for w in ("ultima", "ultimo", "reciente", "anterior")):
            extra = (
                "\n⚠️  INSTRUCCIÓN ESPECIAL: La pregunta pide información sobre la sesión "
                "más reciente. Identifica la sesión con la fecha más alta en el contexto "
                "y elabora un resumen exhaustivo de ella.\n"
            )
        else:
            extra = (
                "\n⚠️  INSTRUCCIÓN DE CALIDAD: Sé exhaustivo. Si tienes transcripciones, "
                "extrae todos los temas tratados, quién intervino y qué se acordó. "
                "Un buen resumen tiene al menos 300 palabras.\n"
            )

        return (
            f"{SYSTEM_PROMPT}\n\n"
            f"{bar}\n"
            f"CÁMARA: {cam_label}\n"
            f"PREGUNTA: {question}\n"
            f"{extra}"
            f"{bar}\n\n"
            f"CONTEXTO DOCUMENTAL:\n{context}\n\n"
            f"{bar}\n"
            f"RESPUESTA (basada exclusivamente en el contexto, exhaustiva y detallada):\n"
        )

    # ─────────────────────────────────────────────────────────
    # GENERACIÓN CON GEMINI
    # ─────────────────────────────────────────────────────────
    def _generate(self, prompt: str) -> Tuple[str, Optional[str]]:
        last_err = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = self.client.models.generate_content(
                    model=self.model, contents=prompt
                )
                text = (getattr(resp, "text", "") or "").strip()
                if not text:
                    return "El modelo no generó respuesta. Reformule la pregunta.", "empty_response"
                logger.info("Respuesta OK — intento %d, %d chars", attempt, len(text))
                return text, None

            except genai_errors.APIError as exc:
                status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
                logger.warning("APIError intento %d — status=%s", attempt, status)
                if status == 400:
                    return "Solicitud inválida. Reformule la pregunta.", "api_400"
                if status in (401, 403):
                    return (
                        "Error de autenticación con Gemini. "
                        "Verifique GEMINI_API_KEY en Vercel.",
                        "api_auth",
                    )
                if status == 429:
                    return "Límite de cuota alcanzado (429). Intente en unos segundos.", "api_429"
                last_err = exc

            except Exception as exc:
                logger.exception("Error inesperado intento %d", attempt)
                last_err = exc

            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BASE * (2 ** (attempt - 1)))

        return (
            f"El servicio de IA no responde tras {MAX_RETRIES} intentos. "
            f"Error: {type(last_err).__name__}.",
            "max_retries",
        )

    # ─────────────────────────────────────────────────────────
    # HANDLERS DE INVENTARIO (SIN IA)
    # ─────────────────────────────────────────────────────────
    def _handle_list_commissions(self) -> AgentResponse:
        """Lista todas las comisiones sin llamar a Gemini."""
        cam_label = "Senado" if self.camara == "senado" else "Cámara de Diputados"
        result: Dict[str, List[str]] = {}

        for group in ("Permanentes", "Otras", "Unidas"):
            try:
                rows  = self.store.list_commissions(group)
                names = sorted(
                    r.get("commission_name") or r.get("nombre") or str(r)
                    for r in (rows or [])
                )
                if names:
                    result[group] = names
            except Exception:
                repo = getattr(self.store, "data_repo_dir", "")
                gdir = os.path.join(repo, group)
                if os.path.isdir(gdir):
                    names = sorted(
                        n for n in os.listdir(gdir)
                        if os.path.isdir(os.path.join(gdir, n))
                    )
                    if names:
                        result[group] = names

        if not result:
            return AgentResponse(
                answer=f"No encontré comisiones en el repositorio del {cam_label}.",
                chamber_used=self.camara,
            )

        total = sum(len(v) for v in result.values())
        lines = [f"**Comisiones disponibles — {cam_label}** ({total} en total)\n"]
        for group, names in result.items():
            lines.append(f"\n**{group}** ({len(names)})")
            for n in names:
                lines.append(f"- {n}")

        return AgentResponse(
            answer="\n".join(lines),
            hits_found=total,
            chamber_used=self.camara,
        )

    def _handle_commission_inventory(self, question: str) -> AgentResponse:
        """Cuenta documentos disponibles de una comisión específica."""
        cam_label  = "Senado" if self.camara == "senado" else "Cámara de Diputados"
        commission = self._find_commission(self.store, question)

        if not commission:
            return AgentResponse(
                answer=(
                    "No identifiqué una comisión específica en tu pregunta. "
                    "Menciona su nombre completo, por ejemplo: 'Comisión de Salud'."
                ),
                chamber_used=self.camara,
            )

        group, name = commission
        repo = getattr(self.store, "data_repo_dir", "")
        base = os.path.join(repo, group, name)

        n_transcripts = 0
        for td_name in TRANSCRIPT_DIRS:
            for td in [os.path.join(base, td_name),
                       os.path.join(base, "sesiones_detail", td_name)]:
                if os.path.isdir(td):
                    n_transcripts += len(glob.glob(os.path.join(td, "*.txt")))

        n_jsons = 0
        sd = os.path.join(base, "sesiones_detail")
        if os.path.isdir(sd):
            n_jsons = len(glob.glob(os.path.join(sd, "*.json")))

        n_sessions = 0
        hist = os.path.join(base, "historial.csv")
        if os.path.exists(hist):
            n_sessions = len(_read_csv(hist))

        total_content = n_transcripts + n_jsons
        lines = [
            f"**Inventario — {cam_label}**",
            f"**Comisión:** {name} ({group})\n",
            f"- Transcripciones disponibles: **{n_transcripts}**",
            f"- Registros de sesión (JSON): **{n_jsons}**",
            f"- Sesiones en historial: **{n_sessions}**",
            f"\n_Documentos con contenido legible: {total_content}_",
        ]

        if total_content == 0:
            lines.append(
                "\n⚠️ Esta comisión no tiene transcripciones ni detalles de sesión disponibles. "
                "Solo se puede consultar el historial y la nómina de integrantes."
            )

        return AgentResponse(
            answer="\n".join(lines),
            chamber_used=self.camara,
        )