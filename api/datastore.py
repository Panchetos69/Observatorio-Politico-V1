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


class DataStore:
    def __init__(self, data_repo_dir: str, kom_dir: str):
        self.data_repo_dir = os.path.abspath(data_repo_dir)
        self.kom_dir = os.path.abspath(kom_dir)
        
        print(f"[DataStore] Initialized")
        print(f"  data_repo_dir: {self.data_repo_dir}")
        print(f"  kom_dir: {self.kom_dir}")
    
    # -----------------------------
    # KOM Profile (Perfil de Congresistas)
    # -----------------------------
    def get_kom_profile(self, slug: str) -> Optional[dict]:
        """Busca un perfil KOM por slug (nombre o ID)"""
        base = self.kom_dir
        if not os.path.isdir(base):
            return None

        slug_n = (slug or "").strip().lower()
        if not slug_n:
            return None

        # Buscar en archivos JSON del directorio KOM
        for p in glob.glob(os.path.join(base, "*.json")):
            try:
                with open(p, "r", encoding="utf-8-sig", errors="ignore") as f:
                    obj = json.load(f)
                if not isinstance(obj, dict):
                    continue

                name = (obj.get("nombre") or obj.get("name") or "").strip().lower()
                pid = str(obj.get("id") or obj.get("pid") or "").strip().lower()

                if slug_n == pid or (name and slug_n in name):
                    return obj
            except Exception:
                continue

        return None

    # -----------------------------
    # Paths Comisiones
    # -----------------------------
    def commission_dir(self, group: str, commission_name: str) -> str:
        return os.path.join(self.data_repo_dir, group, commission_name)

    def historial_path(self, group: str, commission_name: str) -> str:
        return os.path.join(self.commission_dir(group, commission_name), "historial.csv")

    def integrantes_path(self, group: str, commission_name: str) -> str:
        return os.path.join(self.commission_dir(group, commission_name), "integrantes.json")

    def find_transcript_path(self, group: str, commission_name: str, sid: str) -> Optional[str]:
        base = self.commission_dir(group, commission_name)
        candidates = [
            os.path.join(base, "transcripts", f"{sid}.txt"),
            os.path.join(base, "txt", f"{sid}.txt"),
        ]
        for p in candidates:
            if os.path.exists(p):
                return p
        return None

    # -----------------------------
    # Listados Comisiones / Sesiones
    # -----------------------------
    def list_commissions(self, group: str, q: str = "") -> List[dict]:
        group_dir = os.path.join(self.data_repo_dir, group)
        if not os.path.isdir(group_dir):
            return []

        qn = (q or "").strip().lower()
        out: List[dict] = []
        
        # Listamos carpetas y aseguramos que existe
        try:
            items = sorted(os.listdir(group_dir))
        except Exception:
            return []

        for name in items:
            full = os.path.join(group_dir, name)
            if not os.path.isdir(full):
                continue
            if qn and qn not in name.lower():
                continue

            rows = _safe_read_csv_dicts(os.path.join(full, "historial.csv"))
            out.append(
                {
                    "commission_name": name,
                    "nombre": name,
                    "group": group,
                    "total_sessions": len(rows),
                }
            )
        return out

    def get_commission_sessions(self, group: str, commission_name: str) -> dict:
        hist_path = self.historial_path(group, commission_name)

        if not os.path.exists(hist_path):
            return {"success": False, "error": "No se encontró historial.csv"}

        rows = _safe_read_csv_dicts(hist_path)

        sessions: List[dict] = []
        years_set = set()

        # --- FIX IMPORTANTE: Asegurar que el año actual (2026) exista ---
        # Esto evita que la web falle si no hay sesiones registradas este año aún.
        try:
            years_set.add(datetime.now().year)
        except:
            pass # Si falla datetime por alguna razón, seguimos
        # ----------------------------------------------------------------

        for r in rows:
            año_raw = (r.get("Año") or r.get("año") or r.get("Ano") or r.get("ano") or "").strip()
            mes = (r.get("Mes") or r.get("mes") or "").strip()
            sid = (r.get("ID") or r.get("Id") or r.get("id") or "").strip()
            fecha = (r.get("Fecha") or r.get("fecha") or "").strip()
            estado = (r.get("Estado") or r.get("estado") or "").strip()
            citacion = (r.get("Citacion") or r.get("Citación") or r.get("citacion") or "").strip()
            acta = (r.get("Acta") or r.get("acta") or "").strip()
            cuenta = (r.get("Cuenta") or r.get("cuenta") or "").strip()

            year: Optional[int] = None
            if año_raw and str(año_raw).strip().isdigit():
                year = int(str(año_raw).strip())

            if year is None and fecha:
                parts = fecha.replace(",", " ").split()
                for part in parts:
                    if part.isdigit() and len(part) == 4:
                        year = int(part)
                        break

            if year is not None:
                years_set.add(year)

            if sid or fecha:
                sessions.append(
                    {
                        "ID": sid,
                        "Año": str(year) if year is not None else año_raw,
                        "anio": year,
                        "Mes": mes,
                        "Fecha": fecha,
                        "Estado": estado,
                        "Citacion": citacion,
                        "Acta": acta,
                        "Cuenta": cuenta,
                        "transcript": bool(sid and self.find_transcript_path(group, commission_name, sid)),
                    }
                )

        years = sorted(years_set, reverse=True)
        # Usamos la clave como string para compatibilidad con JSON
        by_year: Dict[str, List[dict]] = {str(y): [] for y in years}

        for s in sessions:
            y = s.get("anio")
            # Intentamos insertar usando el entero convertido a string
            if isinstance(y, int) and str(y) in by_year:
                by_year[str(y)].append(s)
            else:
                # Fallback por si el año vino como texto
                y2 = (s.get("Año") or "").strip()
                if y2.isdigit() and y2 in by_year:
                    by_year[y2].append(s)

        return {
            "success": True,
            "commission": {
                "group": group,
                "commission_name": commission_name,
                "meta": {"nombre": commission_name},
                "years": years,
                "sessions_by_year": by_year,
            },
        }
    
    # -----------------------------
    # Noticias - CON LOGGING DETALLADO
    # -----------------------------
    def news_feed(self, source: str, q: str = "", limit: int = 200) -> List[Dict]:
        """
        Lee noticias desde DIARIO_OFICIAL_EXPORT
        Retorna lista de diccionarios con las noticias
        """
        print(f"\n[news_feed] === INICIO ===")
        print(f"[news_feed] source={source}, q='{q}', limit={limit}")
        
        qn = (q or "").strip().lower()

        if source != "diario_oficial":
            print(f"[news_feed] Source '{source}' no soportado, retornando vacío")
            if source == "camara_senado":
                return []
            return []

        # Calcular ruta a DIARIO_OFICIAL_EXPORT
        project_dir = os.path.abspath(os.path.join(self.kom_dir, ".."))
        diario_dir = os.path.join(project_dir, "DIARIO_OFICIAL_EXPORT")
        
        print(f"[news_feed] kom_dir: {self.kom_dir}")
        print(f"[news_feed] project_dir: {project_dir}")
        print(f"[news_feed] diario_dir: {diario_dir}")
        print(f"[news_feed] Directory exists: {os.path.isdir(diario_dir)}")
        
        if not os.path.isdir(diario_dir):
            print(f"[news_feed] ❌ Directorio no encontrado!")
            print(f"[news_feed] Verificar que exista: {diario_dir}")
            return []

        # Listar archivos en el directorio
        try:
            all_files = os.listdir(diario_dir)
            print(f"[news_feed] Archivos en directorio ({len(all_files)}):")
            for f in all_files:
                print(f"  - {f}")
        except Exception as e:
            print(f"[news_feed] ❌ Error listando directorio: {e}")
            return []

        # Buscar archivos JSON (excluyendo logs)
        json_candidates = [
            p for p in glob.glob(os.path.join(diario_dir, "*.json"))
            if "log" not in os.path.basename(p).lower()
        ]
        csv_candidates = glob.glob(os.path.join(diario_dir, "*.csv"))

        print(f"[news_feed] JSON candidates: {len(json_candidates)}")
        for jf in json_candidates:
            print(f"  - {os.path.basename(jf)}")
        
        print(f"[news_feed] CSV candidates: {len(csv_candidates)}")
        for cf in csv_candidates:
            print(f"  - {os.path.basename(cf)}")

        candidates = json_candidates if json_candidates else csv_candidates
        if not candidates:
            print(f"[news_feed] ❌ No se encontraron archivos JSON/CSV válidos")
            return []

        # Seleccionar el archivo más reciente
        path = max(candidates, key=lambda p: os.path.getmtime(p))
        print(f"[news_feed] ✓ Archivo seleccionado: {os.path.basename(path)}")
        print(f"[news_feed]   Tamaño: {os.path.getsize(path):,} bytes")

        raw: List[dict] = []

        # Leer archivo JSON
        if path.lower().endswith(".json"):
            print(f"[news_feed] Leyendo como JSON...")
            try:
                with open(path, "r", encoding="utf-8-sig", errors="ignore") as f:
                    content = f.read().strip()

                print(f"[news_feed] Contenido leído: {len(content)} caracteres")

                if content.startswith("["):
                    print(f"[news_feed] Formato: JSON Array")
                    data = json.loads(content)
                    if isinstance(data, list):
                        raw = [x for x in data if isinstance(x, dict)]
                        print(f"[news_feed] ✓ Parseado como array: {len(raw)} items")
                else:
                    print(f"[news_feed] Formato: JSONL (línea por línea)")
                    lines = content.splitlines()
                    print(f"[news_feed] Total líneas: {len(lines)}")
                    
                    for i, line in enumerate(lines, 1):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                            if isinstance(obj, dict):
                                raw.append(obj)
                        except Exception as e:
                            print(f"[news_feed] ⚠️  Error en línea {i}: {e}")
                            continue
                    
                    print(f"[news_feed] ✓ Parseado {len(raw)} objetos de {len(lines)} líneas")
                    
            except Exception as e:
                print(f"[news_feed] ❌ Error leyendo JSON {path}: {e}")
                import traceback
                traceback.print_exc()
                return []

        # Leer archivo CSV
        elif path.lower().endswith(".csv"):
            print(f"[news_feed] Leyendo como CSV...")
            raw = _safe_read_csv_dicts(path)
            print(f"[news_feed] ✓ Leído CSV: {len(raw)} filas")

        if not raw:
            print(f"[news_feed] ❌ No se pudieron parsear datos del archivo")
            return []

        # Mostrar muestra de datos
        if raw:
            print(f"[news_feed] Muestra del primer item:")
            sample = raw[0]
            for key in list(sample.keys())[:10]:
                value = str(sample.get(key, ""))[:80]
                print(f"  {key}: {value}")
            
            # Mostrar todas las claves disponibles
            all_keys = set()
            for item in raw:
                all_keys.update(item.keys())
            print(f"[news_feed] Claves disponibles: {', '.join(sorted(all_keys))}")

        # Procesar y filtrar items
        out: List[Dict] = []
        for it in raw:
            titulo = (it.get("titulo") or it.get("title") or it.get("Título") or it.get("Titulo") or "").strip()
            fecha = (it.get("fecha") or it.get("date") or it.get("Fecha") or "").strip()

            pdf_url = (it.get("pdf_url") or it.get("url") or it.get("link") or it.get("pdf") or "").strip()
            edicion_url = (it.get("edicion_url") or it.get("edition_url") or it.get("edicion") or "").strip()

            cve = (it.get("cve") or it.get("CVE") or "").strip()
            edition = (it.get("edition") or it.get("edicion_num") or it.get("Edition") or "").strip()
            tab = (it.get("tab") or it.get("Tab") or "").strip()

            # Filtrar por query
            hay = f"{titulo} {tab} {cve}".lower()
            if qn and qn not in hay:
                continue

            out.append(
                {
                    "titulo": titulo,
                    "title": titulo,
                    "fecha": fecha,
                    "date": fecha,
                    "url": pdf_url,
                    "pdf_url": pdf_url,
                    "edicion_url": edicion_url,
                    "cve": cve,
                    "edition": edition,
                    "tab": tab,
                    "source": "diario_oficial",
                }
            )

        print(f"[news_feed] Items después de filtrar: {len(out)}")

        # Ordenar por fecha (más reciente primero)
        def key_dt(x: Dict) -> tuple:
            f = (x.get("fecha") or "").strip()
            try:
                d, m, y = f.split("-")
                return (int(y), int(m), int(d))
            except Exception:
                return (0, 0, 0)

        out.sort(key=key_dt, reverse=True)
        
        final_count = min(len(out), limit)
        print(f"[news_feed] ✓ Retornando {final_count} items (de {len(out)} totales)")
        print(f"[news_feed] === FIN ===\n")
        
        return out[:limit]

    # -----------------------------
    # Politicos
    # -----------------------------
    def list_politicians(self, q: str = "", chamber: str = "all") -> List[dict]:
        qn = (q or "").strip().lower()
        chamber_filter = (chamber or "all").strip().lower()
        out: Dict[str, dict] = {}

        if not os.path.isdir(self.data_repo_dir):
            return []

        for group in ["Permanentes", "Otras", "Unidas"]:
            group_dir = os.path.join(self.data_repo_dir, group)
            if not os.path.isdir(group_dir):
                continue
            for commission_name in os.listdir(group_dir):
                p = self.integrantes_path(group, commission_name)
                data = _safe_read_json(p)
                if not data:
                    continue

                members = data
                if isinstance(data, dict):
                    members = data.get("integrantes") or data.get("members") or data.get("items") or []
                if not isinstance(members, list):
                    continue

                for m in members:
                    if not isinstance(m, dict):
                        continue
                    nombre = (m.get("nombre") or m.get("name") or "").strip()
                    if not nombre:
                        continue
                    if qn and qn not in nombre.lower():
                        continue
                    
                    member_chamber = (m.get("chamber") or m.get("camara") or "").strip().lower()
                    
                    if chamber_filter != "all" and member_chamber != chamber_filter:
                        continue
                    
                    pid = str(m.get("id") or m.get("pid") or nombre)
                    key = f"{pid}"
                    if key not in out:
                        out[key] = {
                            "id": pid,
                            "nombre": nombre,
                            "cargo": m.get("cargo") or m.get("role") or "",
                            "chamber": member_chamber,
                            "url_ficha": m.get("url_ficha") or m.get("url") or "",
                        }
        return list(out.values())

    # -----------------------------
    # Actividad - CON FILTRADO POR FECHA
    # -----------------------------
    def activity_feed(self, group: str = "", status: str = "", q: str = "", chamber: str = "", days_back: int = 90) -> List[dict]:
        """
        Retorna actividad legislativa reciente
        """
        groups = [group] if group else ["Permanentes", "Otras", "Unidas"]
        status_n = (status or "").strip().lower()
        qn = (q or "").strip().lower()
        items: List[dict] = []
        
        # Calcular fecha límite (hoy - days_back días)
        fecha_limite = datetime.now() - timedelta(days=days_back)
        print(f"[activity_feed] Filtrando desde: {fecha_limite.strftime('%d-%m-%Y')}")

        for g in groups:
            for c in self.list_commissions(g, q=""):
                cname = c["commission_name"]
                rows = _safe_read_csv_dicts(self.historial_path(g, cname))
                for r in rows:
                    año = (r.get("Año") or r.get("año") or r.get("Ano") or r.get("ano") or "").strip()
                    mes = (r.get("Mes") or r.get("mes") or "").strip()
                    sid = (r.get("ID") or r.get("Id") or r.get("id") or "").strip()
                    fecha = (r.get("Fecha") or r.get("fecha") or "").strip()
                    estado = (r.get("Estado") or r.get("estado") or "").strip()
                    citacion = (r.get("Citacion") or r.get("Citación") or r.get("citacion") or "").strip()

                    # Filtrar por estado
                    if status_n and status_n not in estado.lower():
                        continue
                    
                    # Filtrar por nombre de comisión
                    if qn and qn not in cname.lower():
                        continue

                    # Parsear fecha y filtrar por fecha límite
                    fecha_obj = None
                    if fecha:
                        # Intentar varios formatos de fecha
                        for fmt in ["%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d de %B de %Y"]:
                            try:
                                fecha_limpia = fecha.replace("de ", "")
                                fecha_obj = datetime.strptime(fecha_limpia, fmt)
                                break
                            except ValueError:
                                continue
                        
                        # Si no se pudo parsear la fecha, intentar extraer el año
                        if not fecha_obj:
                            year_match = re.search(r'\b(20\d{2})\b', fecha)
                            if year_match:
                                year = int(year_match.group(1))
                                if year < fecha_limite.year:
                                    continue
                    
                    # Si pudimos parsear la fecha, verificar que sea reciente
                    if fecha_obj and fecha_obj < fecha_limite:
                        continue

                    items.append(
                        {
                            "group": g,
                            "commission": cname,
                            "commission_name": cname,
                            "Año": año,
                            "Mes": mes,
                            "ID": sid,
                            "session_id": sid,
                            "Fecha": fecha,
                            "fecha": fecha,
                            "Estado": estado,
                            "estado": estado,
                            "citacion": citacion,
                            "_fecha_obj": fecha_obj,  # Para ordenar
                        }
                    )

        # Ordenar por fecha (más reciente primero)
        def sort_key(x):
            if x.get("_fecha_obj"):
                return x["_fecha_obj"]
            return datetime(1900, 1, 1)
        
        items.sort(key=sort_key, reverse=True)
        
        # Eliminar el campo temporal _fecha_obj
        for item in items:
            item.pop("_fecha_obj", None)
        
        print(f"[activity_feed] Retornando {len(items)} items recientes (últimos {days_back} días)")
        return items

    # -----------------------------
    # Búsqueda de texto - MEJORADO
    # -----------------------------
    def search_texts(self, query: str, top_k: int = 10) -> List[dict]:
        out: List[dict] = []

        # ---------- Comisiones: transcripts/txt + integrantes.json + historial.csv ----------
        for group in ["Permanentes", "Otras", "Unidas"]:
            group_dir = os.path.join(self.data_repo_dir, group)
            if not os.path.isdir(group_dir):
                continue

            for commission_name in os.listdir(group_dir):
                base = self.commission_dir(group, commission_name)

                # transcripts/txt
                for folder in ["transcripts", "txt"]:
                    td = os.path.join(base, folder)
                    if not os.path.isdir(td):
                        continue
                    for p in glob.glob(os.path.join(td, "*.txt")):
                        text = _read_text(p)
                        s = _score(query, text)
                        if s > 0:
                            out.append({"file": p, "score": s, "snippet": text[:1400]})

                # integrantes.json
                pj = self.integrantes_path(group, commission_name)
                if os.path.exists(pj):
                    obj = _safe_read_json(pj)
                    if obj:
                        text = json.dumps(obj, ensure_ascii=False)
                        s = _score(query, text)
                        if s > 0:
                            out.append({"file": pj, "score": s, "snippet": text[:1400]})

                # historial.csv
                pc = self.historial_path(group, commission_name)
                if os.path.exists(pc):
                    rows = _safe_read_csv_dicts(pc)
                    if rows:
                        text = json.dumps(rows[:200], ensure_ascii=False)
                        s = _score(query, text)
                        if s > 0:
                            out.append({"file": pc, "score": s, "snippet": text[:1400]})

        # ---------- KOM: perfiles congresistas ----------
        kom_base = self.kom_dir
        if os.path.isdir(kom_base):
            # KOM/*.json
            for p in glob.glob(os.path.join(kom_base, "*.json")):
                obj = _safe_read_json(p)
                if obj:
                    text = json.dumps(obj, ensure_ascii=False)
                    s = _score(query, text)
                    if s > 0:
                        out.append({"file": p, "score": s, "snippet": text[:1400]})

            # KOM/profiles/**.json (si existe)
            for p in glob.glob(os.path.join(kom_base, "profiles", "**", "*.json"), recursive=True):
                obj = _safe_read_json(p)
                if obj:
                    text = json.dumps(obj, ensure_ascii=False)
                    s = _score(query, text)
                    if s > 0:
                        out.append({"file": p, "score": s, "snippet": text[:1400]})

        out.sort(key=lambda x: x["score"], reverse=True)
        return out[:top_k]