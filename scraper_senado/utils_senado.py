"""utils_senado.py — Funciones auxiliares del scraper del Senado"""

import re, csv, json, os, time, logging
from datetime import datetime, date
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup
from config_senado import (
    BASE_URL, HEADERS, DELAY, RETRY_DELAY, MAX_RETRIES, TIMEOUT, MESES_ES
)

log = logging.getLogger(__name__)


# ── HTTP ──────────────────────────────────────────────────────
def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def get_soup(url: str, session: requests.Session) -> BeautifulSoup | None:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, timeout=TIMEOUT)
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding or "utf-8"
            return BeautifulSoup(resp.text, "lxml")
        except requests.RequestException as e:
            log.warning(f"[{attempt}/{MAX_RETRIES}] {url} → {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY * attempt)
    log.error(f"FAILED: {url}")
    return None


# ── TEXTO ─────────────────────────────────────────────────────
def clean(text) -> str:
    return " ".join(str(text or "").strip().split())


def abs_url(href: str) -> str:
    if not href:
        return ""
    return href if href.startswith("http") else urljoin(BASE_URL, href)


# ── FECHAS ────────────────────────────────────────────────────
def parse_fecha_es(texto: str) -> tuple[str, str, str, str]:
    """
    '6 enero 2026' → (año='2026', mes='01', fecha_iso='2026-01-06', fecha_texto='6 enero 2026')
    Retorna tupla vacía si no se puede parsear.
    """
    if not texto:
        return ("", "", "", texto)
    t = texto.lower().strip()
    for mes_es, mes_num in MESES_ES.items():
        if mes_es in t:
            nums = re.findall(r'\d+', t)
            if len(nums) >= 2:
                dia  = nums[0].zfill(2)
                anio = nums[-1]
                return (anio, mes_num, f"{anio}-{mes_num}-{dia}", texto)
    # Intentar formatos numéricos
    for fmt, out_fmt in [
        ("%d/%m/%Y", None),
        ("%Y-%m-%d", None),
        ("%d-%m-%Y", None),
    ]:
        try:
            dt = datetime.strptime(t, fmt)
            return (str(dt.year), str(dt.month).zfill(2),
                    dt.strftime("%Y-%m-%d"), texto)
        except ValueError:
            pass
    return ("", "", "", texto)


def fecha_en_rango(fecha_iso: str, desde: str, hasta: str) -> bool:
    try:
        f = datetime.fromisoformat(fecha_iso).date()
        d = datetime.fromisoformat(desde).date() if desde else date.min
        h = datetime.fromisoformat(hasta).date() if hasta else date.max
        return d <= f <= h
    except Exception:
        return True  # incluir si no parsea


# ── REGEX ─────────────────────────────────────────────────────
RE_COMISION_PATH = re.compile(r'/actividad-legislativa/comisiones/(\d+)(?:/(\d+))?$')
RE_SESION        = re.compile(r'/actividad-legislativa/comisiones/\d+/(\d+)$')
RE_SENADOR       = re.compile(r'/senadoras?-y-senadores?/(\d+)')
RE_BOLETIN       = re.compile(r'(\d{4,6}-\d{2})')
RE_HORA          = re.compile(r'\d{2}:\d{2}')


# ── CSV helpers (compatibles con DataStore) ───────────────────
def write_historial_csv(path: str, rows: list[dict]):
    """
    Escribe historial.csv con las columnas exactas que espera DataStore.
    Columnas: Año, Mes, ID, Fecha, Estado, Citacion, Acta, Cuenta
    """
    from config_senado import HISTORIAL_FIELDS
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=HISTORIAL_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    log.info(f"historial.csv → {path}  ({len(rows)} filas)")


def write_integrantes_json(path: str, integrantes: list[dict]):
    """
    Escribe integrantes.json con el formato que espera DataStore.
    {"integrantes": [{nombre, cargo, id, url_ficha, chamber}]}
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = {"integrantes": integrantes}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    log.info(f"integrantes.json → {path}  ({len(integrantes)} personas)")


def write_json(path: str, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def write_txt(path: str, content: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def read_json(path: str):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ── Paginación ────────────────────────────────────────────────
def next_page_url(soup: BeautifulSoup, current_url: str) -> str | None:
    for a in soup.find_all("a"):
        texto = clean(a.get_text()).lower()
        href  = a.get("href", "")
        if any(t in texto for t in ["siguiente", "próximo", "›", "»"]) and href:
            return abs_url(href)
    return None
