"""
selenium_fallback_senado.py
===========================
Fallback con Playwright para obtener comisiones cuando las tabs JS
no se cargan con requests puro.

Instalar:
  pip install playwright
  playwright install chromium
"""

import logging, re, time
from bs4 import BeautifulSoup

from config_senado import COMISIONES_URL, TIPO_A_GROUP, SENADO_PREFIX
from utils_senado import clean, abs_url, RE_COMISION_PATH

log = logging.getLogger(__name__)

_TAB_TEXTOS = {
    "permanentes": "Permanentes",
    "especiales":  "Especiales",
    "mixtas":      "Mixtas",
    "presupuesto": "Presupuesto",
    "unidas":      "Unidas",
}


def obtener_listado_con_browser(tipos: list[str],
                                 headless: bool = True) -> list[dict]:
    """
    Abre el navegador, hace clic en cada tab y extrae los links.
    Retorna la misma estructura que scrape_listado_comisiones().
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.error("Playwright no instalado: pip install playwright && playwright install chromium")
        return []

    comisiones = []
    seen = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
        ))

        log.info(f"[Playwright] Abriendo {COMISIONES_URL}")
        page.goto(COMISIONES_URL, wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(2000)

        for tipo in tipos:
            texto = _TAB_TEXTOS.get(tipo, tipo.capitalize())
            group = TIPO_A_GROUP.get(tipo, "Otras")

            # Intentar clic en la tab
            selectores = [
                f"button:has-text('{texto}')",
                f"a:has-text('{texto}')",
                f"[data-tab='{tipo}']",
                f"[data-target='#{tipo}']",
                f".tab-link:has-text('{texto}')",
                f"li:has-text('{texto}')",
            ]
            for sel in selectores:
                try:
                    el = page.locator(sel).first
                    if el.count() > 0:
                        el.click()
                        page.wait_for_timeout(1500)
                        log.info(f"[Playwright] Tab '{tipo}' clickeada ({sel})")
                        break
                except Exception:
                    continue

            html = page.content()
            soup = BeautifulSoup(html, "lxml")

            for a in soup.find_all("a", href=True):
                href = a["href"]
                m = RE_COMISION_PATH.search(href)
                if not m or m.group(2):   # ignorar URLs con sesión
                    continue
                nombre = clean(a.get_text())
                url    = abs_url(href)
                if not nombre or url in seen:
                    continue
                seen.add(url)
                comisiones.append({
                    "id_comision":    m.group(1),
                    "nombre":         nombre,
                    "tipo":           tipo,
                    "group":          group,
                    "url":            url,
                    "nombre_carpeta": SENADO_PREFIX + nombre,
                })

            log.info(f"[Playwright] [{tipo}] links hasta ahora: "
                     f"{sum(1 for c in comisiones if c['tipo']==tipo)}")

        browser.close()

    log.info(f"[Playwright] Total: {len(comisiones)} comisiones")
    return comisiones
