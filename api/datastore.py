# api/datastore.py
from __future__ import annotations
from vercel_blob import put
import csv
import glob
import json
import os
from typing import Any, Dict, List, Optional
from datetime import datetime, timedelta
import re


def _safe_read_json(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return json.load(f)
    except Exception:
        return None


def _safe_read_csv_dicts(path: str) -> List[dict]:
    """Lee CSV robusto (Windows/UTF-8 con BOM) y limpia keys/values."""
    try:
        with open(path, "r", encoding="utf-8-sig", errors="ignore", newline="") as f:
            reader = csv.DictReader(f)
            rows: List[dict] = []
            for row in reader:
                clean_row: Dict[str, Any] = {}
                for k, v in (row or {}).items():
                    if k is None:
                        continue
                    kk = str(k).replace("\ufeff", "").strip()
                    vv = v.strip() if isinstance(v, str) else v
                    clean_row[kk] = vv
                rows.append(clean_row)
            return rows
    except Exception as e:
        print(f"[csv] Error reading {path}: {e}")
        return []


def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except Exception:
        return ""


def _normalize(s: str) -> str:
    import unicodedata
    return unicodedata.normalize('NFD', s.lower()).encode('ascii', 'ignore').decode('ascii')

def _score(query: str, text: str) -> int:
    q_words = [_normalize(w) for w in query.split() if len(w) >= 2]
    t_norm  = _normalize(text)
    if not q_words:
        return 0
    score = sum(t_norm.count(w) for w in q_words)
    if len(q_words) > 1 and _normalize(query) in t_norm:
        score += 10
    return score


class DataStore:
    def __init__(self, data_repo_dir: str, kom_dir: str, default_chamber: str = "camara"):
        """
        default_chamber: cámara que se asume cuando un integrante.json
                         no tiene el campo 'chamber' o 'camara'.
                         - "camara"  → para store_camara (REPO_V40...)
                         - "senado"  → para store_senado (REPO_SENADO)
        """
        self.data_repo_dir   = os.path.abspath(data_repo_dir)
        self.kom_dir         = os.path.abspath(kom_dir)
        self.default_chamber = default_chamber.strip().lower()

        print(f"[DataStore] Initialized")
        print(f"  data_repo_dir:   {self.data_repo_dir}")
        print(f"  kom_dir:         {self.kom_dir}")
        print(f"  default_chamber: {self.default_chamber}")

    # -----------------------------
    # Paths helpers
    # -----------------------------
    def integrantes_path(self, group: str, commission_name: str) -> str:
        return os.path.join(self.data_repo_dir, group, commission_name, "integrantes.json")

    def historial_path(self, group: str, commission_name: str) -> str:
        return os.path.join(self.data_repo_dir, group, commission_name, "historial.csv")

    # -----------------------------
    # KOM Profile
    # -----------------------------
    def get_kom_profile(self, slug: str) -> Optional[dict]:
        base = self.kom_dir
        if not os.path.isdir(base):
            return None
        for path in glob.glob(os.path.join(base, "**", "*.json"), recursive=True):
            if os.path.splitext(os.path.basename(path))[0] == slug:
                return _safe_read_json(path)
        return None

    # -----------------------------
    # Comisiones
    # -----------------------------
    def list_commissions(self, group: str = "Permanentes", q: str = "") -> List[dict]:
        qn = (q or "").strip().lower()
        group_dir = os.path.join(self.data_repo_dir, group)
        if not os.path.isdir(group_dir):
            return []
        out = []
        for name in sorted(os.listdir(group_dir)):
            if qn and qn not in name.lower():
                continue
            hist = self.historial_path(group, name)
            total = 0
            if os.path.exists(hist):
                rows = _safe_read_csv_dicts(hist)
                total = len(rows)
            out.append({
                "group":           group,
                "commission_name": name,
                "nombre":          name,
                "total_sessions":  total,
            })
        return out

    def get_commission_sessions(self, group: str, commission_name: str) -> dict:
        """
        Lee historial.csv y agrupa sesiones por año.

        FIX "Sin año":
        - Extrae el año desde múltiples campos y formatos de fecha:
            · Campo "Año" explícito
            · Fecha "YYYY-MM-DD" (formato Senado)
            · Fecha "DD-MM-YYYY" (formato Diputados)
            · Cualquier año 20XX embebido en la cadena
        - Sesiones sin año parseable van a bucket "Sin año" (al final),
          en vez de perderse silenciosamente.
        """
        hist_path = self.historial_path(group, commission_name)
        if not os.path.exists(hist_path):
            return {"success": False, "error": f"historial.csv no encontrado para {commission_name}"}

        rows = _safe_read_csv_dicts(hist_path)
        if not rows:
            return {"success": True, "commission": {
                "group": group, "commission_name": commission_name,
                "years": [], "sessions_by_year": {},
            }}

        # Detectar transcripts disponibles (todas las rutas posibles)
        _base_c = os.path.join(self.data_repo_dir, group, commission_name)
        _tdirs = [
            os.path.join(_base_c, "transcripts"),
            os.path.join(_base_c, "txt"),
            os.path.join(_base_c, "sesiones_detail", "Trancripciones"),
            os.path.join(_base_c, "sesiones_detail", "Transcripciones"),
            os.path.join(_base_c, "sesiones_detail", "transcripciones"),
            os.path.join(_base_c, "sesiones_detail", "transcripts"),
        ]
        transcript_ids: set = set()
        for d in _tdirs:
            if os.path.isdir(d):
                for f in os.listdir(d):
                    if f.endswith(".txt"):
                        transcript_ids.add(os.path.splitext(f)[0])

        def _extract_year(row: dict) -> Optional[int]:
            """Extrae el año de una fila del CSV probando múltiples fuentes."""
            # 1) Campo Año explícito
            año_raw = (
                row.get("Año") or row.get("año") or
                row.get("Ano") or row.get("ano") or ""
            ).strip()
            if año_raw.isdigit() and len(año_raw) == 4:
                return int(año_raw)

            # 2) Parsear desde el campo Fecha
            fecha = (row.get("Fecha") or row.get("fecha") or "").strip()
            if not fecha:
                return None

            # Formato YYYY-MM-DD  (Senado)
            m = re.match(r'^(\d{4})-\d{2}-\d{2}', fecha)
            if m:
                return int(m.group(1))

            # Formato DD-MM-YYYY  (Diputados)
            m = re.match(r'^\d{2}-\d{2}-(\d{4})', fecha)
            if m:
                return int(m.group(1))

            # Cualquier año 20XX embebido en la cadena
            m = re.search(r'\b(20\d{2})\b', fecha)
            if m:
                return int(m.group(1))

            return None

        by_year: Dict[str, list] = {}
        years_seen: set = set()

        for row in rows:
            sid = (row.get("ID") or row.get("id") or row.get("Id") or "").strip()
            row["transcript"] = bool(sid and sid in transcript_ids)

            year = _extract_year(row)

            if year is not None:
                yk = str(year)
                years_seen.add(year)
            else:
                yk = "Sin año"

            by_year.setdefault(yk, []).append(row)

        # Años numéricos en orden descendente, "Sin año" siempre al final
        sorted_numeric = sorted(years_seen, reverse=True)
        years_list = [str(y) for y in sorted_numeric]
        if "Sin año" in by_year:
            years_list.append("Sin año")

        return {
            "success": True,
            "commission": {
                "group":            group,
                "commission_name":  commission_name,
                "years":            years_list,
                "sessions_by_year": by_year,
            },
        }

    def find_transcript_path(self, group: str, commission_name: str, sid: str) -> Optional[str]:
        base = os.path.join(self.data_repo_dir, group, commission_name)
        candidates = [
            os.path.join(base, "transcripts",                         f"{sid}.txt"),
            os.path.join(base, "txt",                                 f"{sid}.txt"),
            os.path.join(base, "sesiones_detail", "Trancripciones",  f"{sid}.txt"),
            os.path.join(base, "sesiones_detail", "Transcripciones", f"{sid}.txt"),
            os.path.join(base, "sesiones_detail", "transcripciones", f"{sid}.txt"),
            os.path.join(base, "sesiones_detail", "transcripts",     f"{sid}.txt"),
        ]
        for p in candidates:
            if os.path.exists(p):
                return p
        return None

    # -----------------------------
    # Políticos
    # -----------------------------
    def list_politicians(self, q: str = "", chamber: str = "all") -> List[dict]:
        """
        Devuelve congresistas únicos con sus comisiones.

        FIX: cuando un integrante NO tiene el campo 'chamber'/'camara',
             se usa self.default_chamber como valor por defecto.
             - store_camara tiene default_chamber="camara"  → diputados sin campo aparecen como diputados ✓
             - store_senado  tiene default_chamber="senado" → senadores sin campo aparecen como senadores ✓
        """
        qn = (q or "").strip().lower()
        chamber_filter = (chamber or "all").strip().lower()

        # Normalizar aliases
        if chamber_filter in ("camara", "diputado", "diputados"):
            chamber_filter = "camara"
        elif chamber_filter in ("senate",):
            chamber_filter = "senado"

        out: Dict[str, dict] = {}

        if not os.path.isdir(self.data_repo_dir):
            print(f"[list_politicians] ⚠ data_repo_dir no existe: {self.data_repo_dir}")
            return []

        total_leidos = 0
        total_pasaron_filtro = 0

        for group in ["Permanentes", "Otras", "Unidas"]:
            group_dir = os.path.join(self.data_repo_dir, group)
            if not os.path.isdir(group_dir):
                continue

            for commission_name in sorted(os.listdir(group_dir)):
                p = self.integrantes_path(group, commission_name)
                data = _safe_read_json(p)
                if not data:
                    continue

                members = data
                if isinstance(data, dict):
                    members = (
                        data.get("integrantes")
                        or data.get("members")
                        or data.get("items")
                        or []
                    )
                if not isinstance(members, list):
                    continue

                for m in members:
                    if not isinstance(m, dict):
                        continue

                    nombre = (m.get("nombre") or m.get("name") or "").strip()
                    if not nombre:
                        continue

                    total_leidos += 1

                    # ── CAMPO CHAMBER CON FALLBACK ──────────────────────────
                    raw = (m.get("chamber") or m.get("camara") or "").strip().lower()

                    # Normalizar alias comunes
                    if raw in ("diputado", "diputados"):
                        raw = "camara"
                    elif raw in ("senador", "senadores", "senate"):
                        raw = "senado"

                    # Si está vacío, usar el default de este DataStore
                    member_chamber = raw if raw else self.default_chamber
                    # ────────────────────────────────────────────────────────

                    # Filtrar por cámara
                    if chamber_filter != "all" and member_chamber != chamber_filter:
                        continue

                    # Filtrar por nombre
                    if qn and qn not in nombre.lower():
                        continue

                    total_pasaron_filtro += 1

                    pid = str(m.get("id") or m.get("pid") or nombre)
                    key = f"{member_chamber}::{pid}"

                    comision_entry = {
                        "group":             group,
                        "commission_name":   commission_name,
                        "cargo_en_comision": (m.get("cargo") or "").strip(),
                    }

                    if key not in out:
                        out[key] = {
                            "id":        pid,
                            "nombre":    nombre,
                            "cargo":     (m.get("cargo") or m.get("role") or "").strip(),
                            "chamber":   member_chamber,
                            "url_ficha": m.get("url_ficha") or m.get("url") or "",
                            "comisiones": [comision_entry],
                        }
                    else:
                        existing = out[key]["comisiones"]
                        already  = any(
                            c["commission_name"] == commission_name
                            for c in existing
                        )
                        if not already:
                            existing.append(comision_entry)

        print(f"[list_politicians] store={self.default_chamber} filter={chamber_filter} "
              f"leidos={total_leidos} pasaron={total_pasaron_filtro} únicos={len(out)}")

        return sorted(out.values(), key=lambda x: x["nombre"])

    # -----------------------------
    # Actividad
    def save_kom_profile(self, chamber: str, pol_id: str, data: dict):
        
        blob_path = f"kom/profiles/{chamber}/{pol_id}.json"
        try:
            # Convertimos el diccionario a string JSON
            json_data = json.dumps(data, indent=2, ensure_ascii=False)
            
            # Subimos a Vercel Blob usando el token de entorno
            # El token se lee automáticamente de BLOB_READ_WRITE_TOKEN
            resp = put(blob_path, json_data, {"access": "public"})
            
            print(f"✓ Perfil guardado en Blob: {resp['url']}")
            return resp['url']
        except Exception as e:
            print(f"✗ Error al guardar en Vercel Blob: {e}")
            raise e
    
    # -----------------------------
    def activity_feed(self, group: str = "", status: str = "", q: str = "",
                      chamber: str = "", days_back: int = 180) -> List[dict]:
        groups   = [group] if group else ["Permanentes", "Otras", "Unidas"]
        status_n = (status or "").strip().lower()
        qn       = (q or "").strip().lower()
        items: List[dict] = []

        fecha_limite = datetime.now() - timedelta(days=days_back)
        print(f"[activity_feed] chamber={self.default_chamber} límite={fecha_limite.strftime('%Y-%m-%d')}")

        def _parse_fecha(fecha: str) -> Optional[datetime]:
            """Soporta DD-MM-YYYY (Diputados) y YYYY-MM-DD (Senado)."""
            if not fecha:
                return None
            parts = fecha.strip().split("-")
            if len(parts) != 3:
                return None
            try:
                if len(parts[0]) == 4:          # YYYY-MM-DD
                    return datetime(int(parts[0]), int(parts[1]), int(parts[2]))
                else:                            # DD-MM-YYYY
                    return datetime(int(parts[2]), int(parts[1]), int(parts[0]))
            except (ValueError, IndexError):
                return None

        for g in groups:
            gdir = os.path.join(self.data_repo_dir, g)
            if not os.path.isdir(gdir):
                continue
            for commission_name in sorted(os.listdir(gdir)):
                hist = self.historial_path(g, commission_name)
                if not os.path.exists(hist):
                    continue
                rows = _safe_read_csv_dicts(hist)
                for row in rows:
                    estado = (row.get("Estado") or row.get("estado") or "").strip().upper()
                    if status_n and status_n not in estado.lower():
                        continue
                    fecha_str = (row.get("Fecha") or row.get("fecha") or "").strip()
                    if fecha_str:
                        fecha_dt = _parse_fecha(fecha_str)
                        if fecha_dt and fecha_dt < fecha_limite:
                            continue
                    titulo = (row.get("Nombre") or row.get("nombre")
                              or row.get("Comision") or commission_name or "").strip()
                    if qn and qn not in titulo.lower() and qn not in commission_name.lower():
                        continue
                    items.append({
                        **row,
                        "commission": commission_name,
                        "group":      g,
                        "chamber":    self.default_chamber,
                        "estado":     estado,
                        "fecha":      fecha_str,
                        "citacion":   (row.get("Citacion") or row.get("citacion") or
                                       row.get("URL_Citacion") or "").strip(),
                        "session_id": (row.get("ID") or row.get("id") or "").strip(),
                    })

        def _sort_key(x: dict) -> datetime:
            dt = None
            parts = x.get("fecha", "").split("-")
            if len(parts) == 3:
                try:
                    if len(parts[0]) == 4:
                        dt = datetime(int(parts[0]), int(parts[1]), int(parts[2]))
                    else:
                        dt = datetime(int(parts[2]), int(parts[1]), int(parts[0]))
                except (ValueError, IndexError):
                    pass
            return dt or datetime.min

        items.sort(key=_sort_key, reverse=True)
        print(f"[activity_feed] → {len(items)} items")
        return items

    # -----------------------------
    # Noticias
    # -----------------------------
    def news_feed(self, source: str, q: str = "", limit: int = 200) -> List[Dict]:
        print(f"\n[news_feed] === INICIO === source={source}, q='{q}', limit={limit}")
        qn = (q or "").strip().lower()

        if source != "diario_oficial":
            return []

        project_dir = os.path.abspath(os.path.join(self.kom_dir, ".."))
        diario_dir  = os.path.join(project_dir, "DIARIO_OFICIAL_EXPORT")

        print(f"[news_feed] diario_dir: {diario_dir}")
        print(f"[news_feed] existe: {os.path.isdir(diario_dir)}")

        if not os.path.isdir(diario_dir):
            return []

        candidates = (
            glob.glob(os.path.join(diario_dir, "*.json"))
            + glob.glob(os.path.join(diario_dir, "*.csv"))
        )
        candidates = [c for c in candidates if "log" not in os.path.basename(c).lower()]
        if not candidates:
            return []

        latest = max(candidates, key=os.path.getmtime)
        print(f"[news_feed] leyendo: {os.path.basename(latest)}")

        out: List[dict] = []

        if latest.endswith(".json"):
            raw = _safe_read_json(latest)
            items = raw if isinstance(raw, list) else (raw or {}).get("items", [])
            for it in (items or []):
                titulo      = (it.get("titulo") or it.get("title") or "").strip()
                fecha       = (it.get("fecha") or it.get("date") or "").strip()
                pdf_url     = (it.get("pdf_url") or it.get("url") or "").strip()
                edicion_url = (it.get("edicion_url") or "").strip()
                cve         = (it.get("cve") or "").strip()
                edition     = (it.get("edition") or it.get("edicion") or "").strip()
                tab         = (it.get("tab") or it.get("Tab") or "").strip()
                hay = f"{titulo} {tab} {cve}".lower()
                if qn and qn not in hay:
                    continue
                out.append({
                    "titulo": titulo, "title": titulo,
                    "fecha": fecha,   "date": fecha,
                    "url": pdf_url,   "pdf_url": pdf_url,
                    "edicion_url": edicion_url,
                    "cve": cve, "edition": edition, "tab": tab,
                    "source": "diario_oficial",
                })
        else:
            rows = _safe_read_csv_dicts(latest)
            for row in rows:
                titulo      = (row.get("titulo") or row.get("title") or "").strip()
                fecha       = (row.get("fecha") or row.get("date") or "").strip()
                pdf_url     = (row.get("pdf_url") or row.get("url") or "").strip()
                edicion_url = (row.get("edicion_url") or "").strip()
                cve         = (row.get("cve") or "").strip()
                edition     = (row.get("edition") or "").strip()
                tab         = (row.get("tab") or "").strip()
                hay = f"{titulo} {tab} {cve}".lower()
                if qn and qn not in hay:
                    continue
                out.append({
                    "titulo": titulo, "title": titulo,
                    "fecha": fecha,   "date": fecha,
                    "url": pdf_url,   "pdf_url": pdf_url,
                    "edicion_url": edicion_url,
                    "cve": cve, "edition": edition, "tab": tab,
                    "source": "diario_oficial",
                })

        def key_dt(x: Dict) -> tuple:
            f = (x.get("fecha") or "").strip()
            try:
                d, m_n, y = f.split("-")
                return (int(y), int(m_n), int(d))
            except Exception:
                return (0, 0, 0)

        out.sort(key=key_dt, reverse=True)
        print(f"[news_feed] retornando {min(len(out), limit)} items")
        return out[:limit]

    # -----------------------------
    # Búsqueda de texto
    # -----------------------------
    def _commission_folders(self) -> List[tuple]:
        """Retorna lista de (group, commission_name, base_path) para todas las comisiones."""
        result = []
        for group in ["Permanentes", "Otras", "Unidas"]:
            gdir = os.path.join(self.data_repo_dir, group)
            if not os.path.isdir(gdir):
                continue
            for name in sorted(os.listdir(gdir)):
                base = os.path.join(gdir, name)
                if os.path.isdir(base):
                    result.append((group, name, base))
        return result

    def _read_commission_docs(self, base_path: str, max_per_type: int = 5) -> List[dict]:
        """Lee documentos de una carpeta de comisión y devuelve hits."""
        hits = []

        # 1. Transcripciones
        for td_name in ["Trancripciones", "Transcripciones", "transcripciones", "transcripts", "txt"]:
            td = os.path.join(base_path, td_name)
            if not os.path.isdir(td):
                td = os.path.join(base_path, "sesiones_detail", td_name)
            if os.path.isdir(td):
                for p in sorted(glob.glob(os.path.join(td, "*.txt")), reverse=True)[:max_per_type]:
                    text = _read_text(p)
                    if text.strip():
                        hits.append({"file": p, "score": 100, "snippet": text[:2000]})

        # 2. sesiones_detail JSONs (más recientes primero)
        sd = os.path.join(base_path, "sesiones_detail")
        if os.path.isdir(sd):
            jsons = sorted(glob.glob(os.path.join(sd, "*.json")), reverse=True)[:max_per_type]
            for p in jsons:
                obj = _safe_read_json(p)
                if obj:
                    text = json.dumps(obj, ensure_ascii=False)
                    hits.append({"file": p, "score": 90, "snippet": text[:2000]})

        # 3. historial.csv — todas las filas (snippet ampliado para no perder URLs)
        hist = os.path.join(base_path, "historial.csv")
        if os.path.exists(hist):
            rows = _safe_read_csv_dicts(hist)
            if rows:
                text = json.dumps(rows, ensure_ascii=False)
                hits.append({"file": hist, "score": 80, "snippet": text[:10000]})

        # 4. integrantes.json
        integ = os.path.join(base_path, "integrantes.json")
        if os.path.exists(integ):
            obj = _safe_read_json(integ)
            if obj:
                text = json.dumps(obj, ensure_ascii=False)
                hits.append({"file": integ, "score": 70, "snippet": text[:2000]})

        return hits

    def search_texts(self, query: str, top_k: int = 6) -> List[dict]:
        """
        Búsqueda inteligente:
        1. Si la query menciona una comisión específica → lee directamente esa carpeta
        2. Si es query general → busca por score en todos los documentos
        """
        import unicodedata as _ud

        def _n(s):
            s = (s or "").lower().strip()
            return "".join(c for c in _ud.normalize("NFD", s) if _ud.category(c) != "Mn")

        query_n = _n(query)
        out: List[dict] = []

        # ── Estrategia 1: detectar comisión específica en la query ──
        matched_commission = None
        best_score = 0
        for group, name, base in self._commission_folders():
            name_n = _n(name)
            # Score de similitud: palabras del nombre que aparecen en la query
            words = [w for w in name_n.split() if len(w) >= 4]
            if not words:
                continue
            match_count = sum(1 for w in words if w in query_n)
            ratio = match_count / len(words)
            if ratio > 0.5 and match_count > best_score:
                best_score = match_count
                matched_commission = (group, name, base)

        if matched_commission:
            group, name, base = matched_commission
            hits = self._read_commission_docs(base, max_per_type=top_k)
            # Aplica score adicional por keywords de la query
            for h in hits:
                kw_score = _score(query, h.get("snippet", ""))
                h["score"] = h["score"] + kw_score
            hits.sort(key=lambda x: x["score"], reverse=True)
            return hits[:top_k]

        # ── Estrategia 2: búsqueda general por score ──
        for group, name, base in self._commission_folders():
            all_files = []

            # Transcripciones
            for td_name in ["Trancripciones", "Transcripciones", "transcripciones", "transcripts", "txt"]:
                for td in [os.path.join(base, td_name),
                           os.path.join(base, "sesiones_detail", td_name)]:
                    if os.path.isdir(td):
                        for p in glob.glob(os.path.join(td, "*.txt")):
                            all_files.append(p)

            # sesiones_detail JSONs
            sd = os.path.join(base, "sesiones_detail")
            if os.path.isdir(sd):
                all_files.extend(glob.glob(os.path.join(sd, "*.json")))

            # historial + integrantes
            for fname in ["historial.csv", "integrantes.json"]:
                fp = os.path.join(base, fname)
                if os.path.exists(fp):
                    all_files.append(fp)

            for p in all_files:
                ext = os.path.splitext(p)[1].lower()
                if ext in (".txt",):
                    text = _read_text(p)
                elif ext in (".json",):
                    obj = _safe_read_json(p)
                    text = json.dumps(obj, ensure_ascii=False) if obj else ""
                elif ext in (".csv",):
                    rows = _safe_read_csv_dicts(p)
                    text = json.dumps(rows, ensure_ascii=False) if rows else ""
                else:
                    continue

                s = _score(query, text)
                # Snippet: más grande para CSV (para no perder URLs del historial)
                snippet_limit = 10000 if ext == ".csv" else 2000
                if s > 0:
                    out.append({"file": p, "score": s, "snippet": text[:snippet_limit]})

        # KOM profiles
        if os.path.isdir(self.kom_dir):
            for p in glob.glob(os.path.join(self.kom_dir, "profiles", "**", "*.json"), recursive=True):
                obj = _safe_read_json(p)
                if obj:
                    text = json.dumps(obj, ensure_ascii=False)
                    s = _score(query, text)
                    if s > 0:
                        out.append({"file": p, "score": s, "snippet": text[:2000]})

        out.sort(key=lambda x: x["score"], reverse=True)
        return out[:top_k]