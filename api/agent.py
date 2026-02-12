import os
import google.generativeai as genai
import urllib.parse
import os

def short_label(path: str, store) -> str:
    for base in [store.data_repo_dir, store.kom_dir]:
        if base and os.path.abspath(path).startswith(os.path.abspath(base)):
            rel = os.path.relpath(path, base).replace("\\", "/")
            # recorta para que no sea eterno (últimos 2-3 segmentos)
            parts = rel.split("/")
            return "/".join(parts[-3:]) if len(parts) > 3 else rel
    return os.path.basename(path)

def source_md_link(path: str, store) -> str:
    label = short_label(path, store)
    url = "/api/file?path=" + urllib.parse.quote(path)
    return f"- [{label}]({url})"

class LegislativeAgent:
    def __init__(self, store, gemini_api_key: str):
        self.store = store
        self.ready = bool(gemini_api_key)
        self.client = genai.Client(api_key=gemini_api_key) if self.ready else None

    def ask(self, question: str) -> str:
        if not self.ready:
            return "⚠️ Falta GEMINI_API_KEY en .env"

        hits = self.store.search_texts(question, top_k=6)

        context = ""
        for h in hits:
            context += f"\n---\nFUENTE: {h['file']}\n{h['snippet']}\n"

        prompt = f"""
Eres un analista legislativo.
REGLA CRÍTICA: Responde SOLO usando el CONTEXTO.
Si el contexto no contiene la respuesta, dilo explícitamente y menciona que no hay evidencia.

PREGUNTA: {question}

CONTEXTO:
{context}

RESPUESTA (en español, neutral, con citas de FUENTE):
"""

        try:
            resp = self.client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
            )
            return (resp.text or "").strip()
        except Exception as e:
            return f"❌ Error Gemini: {e}"