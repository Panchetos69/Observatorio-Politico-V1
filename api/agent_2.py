"""
api/agent.py
LegislativeAgent - RAG liviano sobre documentos locales (solo evidencia)
✅ Usa Gemini (google-genai) para redactar, pero SOLO con fragmentos entregados
✅ Recupera evidencia desde DATA_REPO_DIR (transcripts/txt) y la cita
✅ No carga todo a prompt (evita límites de tokens)
"""

from __future__ import annotations

import os
import glob
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple, Dict

# Preferir SDK nuevo
try:
    from google import genai
    from google.genai import types
    USING_NEW_API = True
except Exception:
    USING_NEW_API = False
    genai = None
    types = None


def _read_text(path: str, max_chars: int = 250_000) -> str:
    """Lee texto robusto (UTF-8 ignore) con límite para no reventar memoria."""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read(max_chars)
    except Exception:
        return ""


def _normalize(s: str) -> str:
    return (s or "").strip().lower()


def _keywords(q: str) -> List[str]:
    # palabras de 3+ letras, sin ruido básico
    stop = {"que","qué","cual","cuál","como","cómo","para","por","con","sin","una","uno","unos","unas","del","de","la","el","los","las","y","o","a","en","al","un","es","se","su","sus"}
    words = re.findall(r"[a-záéíóúñ0-9\-]{3,}", (q or "").lower())
    return [w for w in words if w not in stop]


def _score_keywords(words: List[str], text: str) -> int:
    if not words or not text:
        return 0
    t = text.lower()
    score = 0
    for w in words:
        if len(w) < 3:
            continue
        c = t.count(w)
        if c:
            score += min(c, 30)  # cap por palabra
    return score


def _extract_snippets(text: str, words: List[str], max_snippets: int = 6, radius: int = 280) -> List[str]:
    """Extrae fragmentos alrededor de ocurrencias de keywords para dar evidencia compacta."""
    if not text:
        return []
    t_low = text.lower()
    hits: List[Tuple[int, str]] = []
    for w in words:
        if len(w) < 3:
            continue
        for m in re.finditer(re.escape(w), t_low):
            start = max(0, m.start() - radius)
            end = min(len(text), m.end() + radius)
            snippet = text[start:end].replace("\n", " ").strip()
            hits.append((m.start(), snippet))
            if len(hits) >= max_snippets * 3:
                break
        if len(hits) >= max_snippets * 3:
            break

    hits.sort(key=lambda x: x[0])
    uniq: List[str] = []
    for _, sn in hits:
        if sn and all(sn not in u for u in uniq):
            uniq.append(sn)
        if len(uniq) >= max_snippets:
            break

    # fallback
    if not uniq:
        head = text[:8000].replace("\n", " ").strip()
        if head:
            uniq.append(head)
    return uniq


@dataclass
class DocRef:
    path: str
    group: str
    commission: str
    sid: str  # filename sin extensión


class LegislativeAgent:
    """
    Agente que:
    1) Recupera evidencia desde documentos locales (repo histórico)
    2) Le pasa SOLO esa evidencia a Gemini para redactar
    3) Obliga a citar fuentes y no inventar
    """

    def __init__(self, store, gemini_api_key: str):
        self.store = store
        self.api_key = gemini_api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or ""
        self.ready = bool(self.api_key) and USING_NEW_API

        self.client = genai.Client(api_key=self.api_key) if self.ready else None

        # Índice simple de documentos
        self.docs: List[DocRef] = []
        self._build_index()

    def _build_index(self):
        repo = getattr(self.store, "data_repo_dir", "")
        if not repo or not os.path.isdir(repo):
            return

        # buscamos transcripts/txt (los más útiles)
        patterns = [
            os.path.join(repo, "*", "*", "transcripts", "*.txt"),
            os.path.join(repo, "*", "*", "txt", "*.txt"),
        ]
        paths: List[str] = []
        for pat in patterns:
            paths.extend(glob.glob(pat))

        out: List[DocRef] = []
        for p in paths:
            rel = os.path.relpath(p, repo).replace("\\", "/")
            parts = rel.split("/")
            # esperado: group/commission/(transcripts|txt)/sid.txt
            if len(parts) < 4:
                continue
            group = parts[0]
            commission = parts[1]
            sid = os.path.splitext(parts[-1])[0]
            out.append(DocRef(path=p, group=group, commission=commission, sid=sid))

        # dedupe
        uniq = {}
        for d in out:
            uniq[d.path] = d
        self.docs = list(uniq.values())

    def _detect_commission_filter(self, q: str) -> Optional[str]:
        """Heurística: si menciona una comisión por nombre, filtra."""
        qn = _normalize(q)
        if not qn:
            return None
        # matching contra nombres reales de carpetas (comisiones)
        # tomamos set de commissions disponibles
        comms = {d.commission for d in self.docs}
        for c in comms:
            if c and c.lower() in qn:
                return c
        return None

    def _retrieve(self, question: str, k_docs: int = 6) -> Tuple[List[Tuple[DocRef, List[str], int]], List[str]]:
        """
        Retorna top docs con snippets y score.
        También retorna lista de comisiones revisadas (para respuesta 'no encontrado').
        """
        words = _keywords(question)
        if not words:
            words = _keywords(question + " sesión comisión")  # ligera ayuda

        commission_filter = self._detect_commission_filter(question)
        pool = [d for d in self.docs if (not commission_filter or d.commission == commission_filter)]

        # 1) scoring rápido por path string
        qn = _normalize(question)
        prelim: List[Tuple[int, DocRef]] = []
        for d in pool:
            s = 0
            ptxt = f"{d.group} {d.commission} {d.sid}".lower()
            for w in words:
                if w in ptxt:
                    s += 3
            # bonus si el usuario menciona un número tipo id
            nums = re.findall(r"\b\d{2,}\b", qn)
            if nums and any(n in d.sid for n in nums):
                s += 8
            if s:
                prelim.append((s, d))

        # si no hay prelim, igual toma un pool chico aleatorio (en orden) para buscar por contenido
        if prelim:
            prelim.sort(key=lambda x: x[0], reverse=True)
            candidates = [d for _, d in prelim[:60]]
        else:
            candidates = pool[:60]

        # 2) scoring por contenido real
        scored: List[Tuple[int, DocRef, str]] = []
        for d in candidates:
            txt = _read_text(d.path)
            s = _score_keywords(words, txt)
            if s:
                scored.append((s, d, txt))

        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[:k_docs]

        results: List[Tuple[DocRef, List[str], int]] = []
        for s, d, txt in top:
            snippets = _extract_snippets(txt, words)
            results.append((d, snippets, s))

        searched_commissions = sorted({d.commission for d in candidates})
        return results, searched_commissions

    def _system_instruction(self) -> str:
        return (
            "Eres un analista legislativo del Observatorio Político Chile.\n"
            "REGLA CRÍTICA: SOLO puedes usar la información contenida en las FUENTES entregadas.\n"
            "NO uses conocimiento general. NO inventes.\n"
            "Si la respuesta no está en las fuentes, dilo explícitamente y menciona qué comisiones se revisaron.\n"
            "En cada respuesta:\n"
            "1) Cita al menos una fuente (DOCUMENTO + Comisión + ID si aplica)\n"
            "2) Incluye fechas/números EXACTOS si aparecen.\n"
        )

    def ask(self, question: str) -> str:
        if not self.ready:
            return "⚠️ Gemini no está configurado. Revisa GEMINI_API_KEY y requirements (google-genai)."

        retrieved, searched_commissions = self._retrieve(question)

        if not retrieved:
            comms = ", ".join(searched_commissions[:12]) if searched_commissions else "N/A"
            return (
                "🔍 No encontré evidencia en los documentos disponibles para responder.\n\n"
                f"Comisiones revisadas (muestra): {comms}\n"
                "Tip: prueba mencionar la comisión exacta o el ID de sesión (por ejemplo 173) y el tema."
            )

        # construir contexto compacto
        sources_blocks = []
        for d, snippets, score in retrieved:
            header = f"DOCUMENTO: {d.sid}.txt | COMISIÓN: {d.commission} | GRUPO: {d.group} | SCORE: {score}"
            body = "\n- " + "\n- ".join(snippets[:6])
            sources_blocks.append(header + body)

        prompt = (
            "PREGUNTA DEL USUARIO:\n"
            f"{question}\n\n"
            "FUENTES (usa SOLO esto):\n"
            + "\n\n".join(sources_blocks)
            + "\n\n"
            "INSTRUCCIONES:\n"
            "- Responde en español, directo y con viñetas si ayuda.\n"
            "- Cita fuentes en formato: Fuente: <DOCUMENTO> (Comisión <X>). \n"
            "- Si falta un dato específico, dilo y sugiere qué buscar.\n"
        )

        try:
            resp = self.client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.2,
                    max_output_tokens=1200,
                    system_instruction=self._system_instruction(),
                ),
            )
            return (resp.text or "").strip()
        except Exception as e:
            return f"❌ Error Gemini: {str(e)}"
