# ============================================================
#  config_senado.py — Scraper Senado de Chile
#  Compatible con DataStore de OBSERVATORIO-POLITICO
# ============================================================

BASE_URL       = "https://www.senado.cl"
COMISIONES_URL = f"{BASE_URL}/actividad-legislativa/comisiones"

# Mapeo tipo Senado → carpeta group en REPO_V40_HISTORIAL_COMPLETO_V2
# El DataStore busca en "Permanentes", "Otras", "Unidas"
# Agregamos "Senado_Permanentes", etc. para no mezclar con Cámara
TIPO_A_GROUP = {
    "permanentes": "Permanentes",   # misma carpeta que Cámara, el nombre de comisión diferencia
    "especiales":  "Otras",         # Especiales van en Otras (igual que Cámara)
    "mixtas":      "Otras",         # Mixtas también en Otras
    "presupuesto": "Otras",         # Presupuesto en Otras
    "unidas":      "Unidas",        # Unidas = Unidas
}

# Prefijo para distinguir comisiones del Senado vs Cámara en la misma carpeta group
# Ej: "Senado_Comision de Educacion" vs "Comision de Educacion" (Cámara)
SENADO_PREFIX = "Senado_"

# ── HTTP ──────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/121.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-CL,es;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.senado.cl/",
}

DELAY       = 1.5   # segundos entre requests
RETRY_DELAY = 4.0
MAX_RETRIES = 3
TIMEOUT     = 30

# ── Meses en español ─────────────────────────────────────────
MESES_ES = {
    "enero":"01","febrero":"02","marzo":"03","abril":"04",
    "mayo":"05","junio":"06","julio":"07","agosto":"08",
    "septiembre":"09","octubre":"10","noviembre":"11","diciembre":"12",
}

# Campos de historial.csv (igual que Cámara para compatibilidad DataStore)
HISTORIAL_FIELDS = ["Año", "Mes", "ID", "Fecha", "Estado", "Citacion", "Acta", "Cuenta"]
