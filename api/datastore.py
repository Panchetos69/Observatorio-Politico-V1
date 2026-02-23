# api/datastore.py
from __future__ import annotations

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


def _score(query: str, text: str) -> int:
    q = query.lower().split()
    t = text.lower()
    return sum(t.count(w) for w in q if len(w) >= 3)


def _parse_fecha(fecha: str) -> Optional[datetime]:
    """
    Parsea una fecha soportando:
    - DD-MM-YYYY  (Diputados)
    - YYYY-MM-DD  (Senado / ISO)
    - cualquier año 20XX embebido
    Retorna datetime o None.
    """
    fecha = (fecha or "").strip()
    if not fecha:
        return None

    # Formato DD-MM-YYYY
    m = re.match(r'^(\d{1,2})-(\d{1,2})-(\d{4})$', fecha)
    if m:
        try:
            return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            pass

    # Formato YYYY-MM-DD (y variantes con hora)
    m = re.match(r'^(\d{4})-(\d{1,2})-(\d{1,2})', fecha)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass

    # Año embebido como fallback
    m = re.search(r'\b(20\d{2})\b', fecha)
    if m:
        return datetime(int(m.group(1)), 1, 1)

    return None


class DataStore:
    def __init__(self, data_repo_dir: str, kom_dir: str, default_chamber: str = "camara"):
        self.data_repo_dir   = os.path.abspath(data_repo_dir)
        self.kom_dir         = os.path.abspath(kom_dir)
        self.default_chamber = default_chamber.strip().lower()

        print(f"[DataStore] Initialized")
        print(f"  data_repo_dir:   {self.data_repo_dir}")
        print(f"  kom_dir:         {self.kom_dir}")
        print(f"  default_chamber: {self.default_chamber}")

    # ─────────────────────────────────────────
    # Paths helpers
    # ─────────────────────────────────────────
    def integrantes_path(self, group: str, commission_name: str) -> str:
        return os.path.join(self.data_repo_dir, group, commission_name, "integrantes.json")

    def historial_path(self, group: str, commission_name: str) -> str:
        return os.path.join(self.data_repo_dir, group, commission_name, "historial.csv")

    # ─────────────────────────────────────────
    # KOM Profile
    # ─────────────────────────────────────────
    def get_kom_profile(self, slug: str) -> Optional[dict]:
        base = self.kom_dir
        if not os.path.isdir(base):
            return None
        for path in glob.glob(os.path.join(base, "**", "*.json"), recursive=True):
            if os.path.splitext(os.path.basename(path))[0] == slug:
                return _safe_read_json(path)
        return None

    # ─────────────────────────────────────────
    # Comisiones
    # ─────────────────────────────────────────
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

    def list_commission_names(self) -> List[str]:
        """Retorna lista plana de nombres de comisiones para filtros del frontend."""
        names: set = set()
        for group in ["Permanentes", "Otras", "Unidas"]:
            gdir = os.path.join(self.data_repo_dir, group)
            if not os.path.isdir(gdir):
                continue
            for name in os.listdir(gdir):
                if os.path.isdir(os.path.join(gdir, name)):
                    names.add(name)
        return sorted(names)

    def get_commission_sessions(self, group: str, commission_name: str) -> dict:
        """
        Lee historial.csv y agrupa sesiones por año.
        Soporta DD-MM-YYYY (Diputados) y YYYY-MM-DD (Senado).
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

        # Detectar transcripts disponibles
        transcript_ids: set = set()
        for sub in ["transcripts", "txt"]:
            td = os.path.join(self.data_repo_dir, group, commission_name, sub)
            if os.path.isdir(td):
                for f in os.listdir(td):
                    if f.endswith(".txt"):
                        transcript_ids.add(os.path.splitext(f)[0])

        def _extract_year(row: dict) -> Optional[int]:
            año_raw = (
                row.get("Año") or row.get("año") or
                row.get("Ano") or row.get("ano") or ""
            ).strip()
            if año_raw.isdigit() and len(año_raw) == 4:
                return int(año_raw)

            fecha = (row.get("Fecha") or row.get("fecha") or "").strip()
            dt = _parse_fecha(fecha)
            return dt.year if dt else None

        by_year: Dict[str, list] = {}
        years_seen: set = set()

        for row in rows:
            sid = (row.get("ID") or row.get("id") or row.get("Id") or "").strip()
            row["transcript"] = bool(sid and sid in transcript_ids)

            year = _extract_year(row)
            yk = str(year) if year is not None else "Sin año"
            if year is not None:
                years_seen.add(year)

            by_year.setdefault(yk, []).append(row)

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
        for sub in ["transcripts", "txt"]:
            p = os.path.join(self.data_repo_dir, group, commission_name, sub, f"{sid}.txt")
            if os.path.exists(p):
                return p
        return None

    # ─────────────────────────────────────────
    # Políticos
    # ─────────────────────────────────────────
    def list_politicians(self, q: str = "", chamber: str = "all") -> List[dict]:
        """
        Devuelve congresistas únicos con comisiones y período.
        El período se infiere del historial de sesiones de cada comisión.
        """
        qn = (q or "").strip().lower()
        chamber_filter = (chamber or "all").strip().lower()

        if chamber_filter in ("camara", "diputado", "diputados"):
            chamber_filter = "camara"
        elif chamber_filter in ("senate",):
            chamber_filter = "senado"

        out: Dict[str, dict] = {}

        if not os.path.isdir(self.data_repo_dir):
            print(f"[list_politicians] ⚠ data_repo_dir no existe: {self.data_repo_dir}")
            return []

        # Pre-construir índice de años por comisión (para período)
        commission_years: Dict[str, set] = {}

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

                # Obtener años disponibles en el historial de esta comisión (lazy)
                com_key = f"{group}::{commission_name}"
                if com_key not in commission_years:
                    hist = self.historial_path(group, commission_name)
                    years_set: set = set()
                    if os.path.exists(hist):
                        for hr in _safe_read_csv_dicts(hist):
                            fecha = (hr.get("Fecha") or hr.get("fecha") or "").strip()
                            dt = _parse_fecha(fecha)
                            if dt:
                                years_set.add(dt.year)
                    commission_years[com_key] = years_set

                hist_years = commission_years[com_key]
                if hist_years:
                    min_y, max_y = min(hist_years), max(hist_years)
                    com_periodo = f"{min_y}-{max_y}" if min_y != max_y else str(min_y)
                    # Año más reciente para ordenar por período
                    com_max_year = max_y
                else:
                    com_periodo  = ""
                    com_max_year = 0

                for m in members:
                    if not isinstance(m, dict):
                        continue

                    nombre = (m.get("nombre") or m.get("name") or "").strip()
                    if not nombre:
                        continue

                    total_leidos += 1

                    raw = (m.get("chamber") or m.get("camara") or "").strip().lower()
                    if raw in ("diputado", "diputados"):
                        raw = "camara"
                    elif raw in ("senador", "senadores", "senate"):
                        raw = "senado"

                    member_chamber = raw if raw else self.default_chamber

                    if chamber_filter != "all" and member_chamber != chamber_filter:
                        continue

                    if qn and qn not in nombre.lower():
                        continue

                    total_pasaron_filtro += 1

                    pid = str(m.get("id") or m.get("pid") or nombre)
                    key = f"{member_chamber}::{pid}"

                    # Período: preferir campo explícito, si no usar historial
                    periodo_m = (
                        m.get("periodo") or m.get("period") or
                        m.get("legislatura") or m.get("mandato") or ""
                    ).strip() or com_periodo

                    comision_entry = {
                        "group":             group,
                        "commission_name":   commission_name,
                        "cargo_en_comision": (m.get("cargo") or "").strip(),
                        "periodo":           com_periodo,
                    }

                    if key not in out:
                        out[key] = {
                            "id":          pid,
                            "nombre":      nombre,
                            "cargo":       (m.get("cargo") or m.get("role") or "").strip(),
                            "chamber":     member_chamber,
                            "url_ficha":   m.get("url_ficha") or m.get("url") or "",
                            "periodo":     periodo_m,
                            "max_year":    com_max_year,   # para ordenar más recientes primero
                            "comisiones":  [comision_entry],
                        }
                    else:
                        existing = out[key]["comisiones"]
                        already  = any(c["commission_name"] == commission_name for c in existing)
                        if not already:
                            existing.append(comision_entry)
                        # Actualizar max_year y período si es más reciente
                        if com_max_year > out[key].get("max_year", 0):
                            out[key]["max_year"] = com_max_year
                            if com_periodo and not out[key]["periodo"]:
                                out[key]["periodo"] = com_periodo

        print(f"[list_politicians] store={self.default_chamber} filter={chamber_filter} "
              f"leidos={total_leidos} pasaron={total_pasaron_filtro} únicos={len(out)}")

        # Ordenar: más activos recientemente primero (max_year desc), luego nombre
        return sorted(
            out.values(),
            key=lambda x: (-x.get("max_year", 0), x["nombre"])
        )

    # ─────────────────────────────────────────
    # Actividad
    # ─────────────────────────────────────────
    def activity_feed(self, group: str = "", status: str = "", q: str = "",
                      chamber: str = "", days_back: int = 180) -> List[dict]:
        """
        FIX CRÍTICO: usa _parse_fecha() que soporta YYYY-MM-DD (Senado)
        además de DD-MM-YYYY (Diputados).
        Sesiones CITADAS siempre se incluyen aunque sean futuras.
        """
        groups   = [group] if group else ["Permanentes", "Otras", "Unidas"]
        status_n = (status or "").strip().lower()
        qn       = (q or "").strip().lower()
        items: List[dict] = []

        fecha_limite = datetime.now() - timedelta(days=days_back)
        print(f"[activity_feed] chamber={self.default_chamber} status='{status_n}' "
              f"days_back={days_back} límite={fecha_limite.strftime('%Y-%m-%d')}")

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

                    # Filtro estado
                    if status_n and status_n not in estado.lower():
                        continue

                    # Filtro fecha — robusto con _parse_fecha
                    fecha_str = (row.get("Fecha") or row.get("fecha") or "").strip()
                    if fecha_str:
                        fecha_dt = _parse_fecha(fecha_str)
                        if fecha_dt:
                            es_citada = "CITADA" in estado
                            # Citadas futuras siempre se muestran
                            # El resto: respetar ventana de días
                            if not es_citada and fecha_dt < fecha_limite:
                                continue
                            elif es_citada and fecha_dt < fecha_limite:
                                continue  # Citadas muy antiguas también se ocultan

                    # Filtro búsqueda
                    titulo = (
                        row.get("Nombre") or row.get("nombre") or
                        row.get("Comision") or commission_name or ""
                    ).strip()
                    if qn and qn not in titulo.lower() and qn not in commission_name.lower():
                        continue

                    items.append({
                        **row,
                        "commission": commission_name,
                        "group":      g,
                        "chamber":    self.default_chamber,
                        "estado":     estado,
                        "fecha":      fecha_str,
                        "citacion":   (
                            row.get("Citacion") or row.get("citacion") or
                            row.get("URL_Citacion") or row.get("url_citacion") or ""
                        ).strip(),
                        "session_id": (
                            row.get("ID") or row.get("id") or row.get("Id") or ""
                        ).strip(),
                    })

        def _sort_key(x: dict):
            dt = _parse_fecha(x.get("fecha", ""))
            return dt or datetime.min

        items.sort(key=_sort_key, reverse=True)
        print(f"[activity_feed] → {len(items)} items")
        return items

    # ─────────────────────────────────────────
    # Noticias
    # ─────────────────────────────────────────
    def news_feed(self, source: str, q: str = "", limit: int = 200) -> List[Dict]:
        print(f"\n[news_feed] source={source}, q='{q}', limit={limit}")
        qn = (q or "").strip().lower()

        if source != "diario_oficial":
            return []

        project_dir = os.path.abspath(os.path.join(self.kom_dir, ".."))
        diario_dir  = os.path.join(project_dir, "DIARIO_OFICIAL_EXPORT")

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
        out: List[dict] = []

        def _row_to_item(r: dict) -> dict:
            return {
                "titulo":      (r.get("titulo") or r.get("title") or "").strip(),
                "title":       (r.get("titulo") or r.get("title") or "").strip(),
                "fecha":       (r.get("fecha") or r.get("date") or "").strip(),
                "date":        (r.get("fecha") or r.get("date") or "").strip(),
                "url":         (r.get("pdf_url") or r.get("url") or "").strip(),
                "pdf_url":     (r.get("pdf_url") or r.get("url") or "").strip(),
                "edicion_url": (r.get("edicion_url") or "").strip(),
                "cve":         (r.get("cve") or "").strip(),
                "edition":     (r.get("edition") or r.get("edicion") or "").strip(),
                "tab":         (r.get("tab") or r.get("Tab") or "").strip(),
                "source":      "diario_oficial",
            }

        if latest.endswith(".json"):
            raw = _safe_read_json(latest)
            items = raw if isinstance(raw, list) else (raw or {}).get("items", [])
            for it in (items or []):
                item = _row_to_item(it)
                hay = f"{item['titulo']} {item['tab']} {item['cve']}".lower()
                if qn and qn not in hay:
                    continue
                out.append(item)
        else:
            for row in _safe_read_csv_dicts(latest):
                item = _row_to_item(row)
                hay = f"{item['titulo']} {item['tab']} {item['cve']}".lower()
                if qn and qn not in hay:
                    continue
                out.append(item)

        out.sort(key=lambda x: _parse_fecha(x.get("fecha", "")) or datetime.min, reverse=True)
        return out[:limit]

    # ─────────────────────────────────────────
    # Búsqueda de texto
    # ─────────────────────────────────────────
    def search_texts(self, query: str, top_k: int = 6) -> List[dict]:
        out: List[dict] = []
        if not os.path.isdir(self.data_repo_dir):
            return out

        for group in ["Permanentes", "Otras", "Unidas"]:
            gdir = os.path.join(self.data_repo_dir, group)
            if not os.path.isdir(gdir):
                continue
            for commission_name in sorted(os.listdir(gdir)):
                for sub in ["transcripts", "txt"]:
                    td = os.path.join(gdir, commission_name, sub)
                    if not os.path.isdir(td):
                        continue
                    for p in glob.glob(os.path.join(td, "*.txt")):
                        text = _read_text(p)
                        s = _score(query, text)
                        if s > 0:
                            out.append({"file": p, "score": s, "snippet": text[:1400]})

                pj = self.integrantes_path(group, commission_name)
                if os.path.exists(pj):
                    obj = _safe_read_json(pj)
                    if obj:
                        text = json.dumps(obj, ensure_ascii=False)
                        s = _score(query, text)
                        if s > 0:
                            out.append({"file": pj, "score": s, "snippet": text[:1400]})

                pc = self.historial_path(group, commission_name)
                if os.path.exists(pc):
                    rows = _safe_read_csv_dicts(pc)
                    if rows:
                        text = json.dumps(rows[:200], ensure_ascii=False)
                        s = _score(query, text)
                        if s > 0:
                            out.append({"file": pc, "score": s, "snippet": text[:1400]})

        if os.path.isdir(self.kom_dir):
            for p in glob.glob(os.path.join(self.kom_dir, "*.json")):
                obj = _safe_read_json(p)
                if obj:
                    text = json.dumps(obj, ensure_ascii=False)
                    s = _score(query, text)
                    if s > 0:
                        out.append({"file": p, "score": s, "snippet": text[:1400]})
            for p in glob.glob(os.path.join(self.kom_dir, "profiles", "**", "*.json"), recursive=True):
                obj = _safe_read_json(p)
                if obj:
                    text = json.dumps(obj, ensure_ascii=False)
                    s = _score(query, text)
                    if s > 0:
                        out.append({"file": p, "score": s, "snippet": text[:1400]})

        out.sort(key=lambda x: x["score"], reverse=True)
        return out[:top_k]

    # ─────────────────────────────────────────
    # Upload
    # ─────────────────────────────────────────
    def save_upload(self, filename: str, raw: bytes) -> str:
        import uuid
        uploads_dir = os.path.join(os.path.dirname(self.data_repo_dir), "uploads")
        os.makedirs(uploads_dir, exist_ok=True)
        ext  = os.path.splitext(filename or "file")[-1].lower() or ".bin"
        name = f"{uuid.uuid4().hex[:8]}{ext}"
        dest = os.path.join(uploads_dir, name)
        with open(dest, "wb") as f:
            f.write(raw)
        return dest