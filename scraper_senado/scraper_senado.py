"""
scraper_senado.py
=================
Scraping del Senado de Chile → genera archivos compatibles con DataStore.

Por cada comisión crea en REPO_V40_HISTORIAL_COMPLETO_V2/:
  {group}/{Senado_NombreComision}/
      historial.csv           ← Año, Mes, ID, Fecha, Estado, Citacion, Acta, Cuenta
      integrantes.json        ← {integrantes:[{nombre,cargo,id,url_ficha,chamber}]}
      sesiones_meta.json      ← metadata extra (lugar, puntos, presentaciones)
      transcripts/
          {id_sesion}.txt     ← texto de puntos + presentaciones de cada sesión

Notas de la estructura del sitio:
- Listado de comisiones: /actividad-legislativa/comisiones
  Las tabs (Permanentes/Especiales/…) cambian el DOM sin cambiar la URL.
  El HTML completo puede contener los 5 listados embebidos.
  Si no, se intenta con ?tab=tipo.

- Comisión individual: /actividad-legislativa/comisiones/{id}
  Barra lateral: Integrantes | Citaciones | Sesiones | Temas | Informes

- Sesión: /actividad-legislativa/comisiones/{id_com}/{id_ses}
  Tab "Puntos": lista puntos con boletín, tema, aspectos, acuerdos
  Tab "Presentaciones ante comisión": tabla PDF con título y organización

- Sesiones se listan en: /actividad-legislativa/comisiones/{id}/sesiones
"""

from __future__ import annotations

import re
import time
import logging
from urllib.parse import urljoin, urlencode

import requests
from bs4 import BeautifulSoup

from config_senado import (
    BASE_URL, COMISIONES_URL, TIPO_A_GROUP, SENADO_PREFIX, DELAY
)
from utils_senado import (
    get_soup, clean, abs_url, parse_fecha_es, fecha_en_rango,
    next_page_url, RE_COMISION_PATH, RE_SESION, RE_SENADOR,
    RE_BOLETIN, RE_HORA,
)

log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════
# 1. LISTADO DE COMISIONES
# ══════════════════════════════════════════════════════════════

def scrape_listado_comisiones(session: requests.Session,
                               tipos: list[str]) -> list[dict]:
    """
    Retorna lista de dicts:
      {id_comision, nombre, tipo, group, url, nombre_carpeta}

    nombre_carpeta = 'Senado_' + nombre  → carpeta en REPO
    group          = mapeado desde TIPO_A_GROUP
    """
    log.info(f"Obteniendo listado de comisiones — tipos: {tipos}")

    # Cargar la página principal (puede contener los 5 tabs embebidos)
    soup_main = get_soup(COMISIONES_URL, session)
    time.sleep(DELAY)

    todas: list[dict] = []

    for tipo in tipos:
        comisiones = _extraer_tipo(soup_main, tipo, session)
        log.info(f"  [{tipo}] {len(comisiones)} comisiones")
        todas.extend(comisiones)

    # Deduplicar por URL
    seen, dedup = set(), []
    for c in todas:
        if c["url"] not in seen:
            seen.add(c["url"])
            dedup.append(c)

    log.info(f"Total comisiones únicas: {len(dedup)}")
    return dedup


def _extraer_tipo(soup_main: BeautifulSoup | None,
                  tipo: str,
                  session: requests.Session) -> list[dict]:
    """Extrae comisiones de un tipo, probando múltiples estrategias."""
    group = TIPO_A_GROUP.get(tipo, "Otras")

    # Estrategia 1: HTML principal (puede tener tabs embebidos)
    if soup_main:
        # Buscar contenedor específico del tipo
        contenedor = _encontrar_contenedor_tab(soup_main, tipo)
        if contenedor:
            links = _links_comision_de(contenedor, tipo, group)
            if links:
                return links

        # Si no hay tab específica, todos los links del page (primer tipo = fallback)
        if tipo == "permanentes":
            links = _links_comision_de(soup_main, tipo, group)
            if links:
                return links

    # Estrategia 2: URL con parámetro
    for url_alt in [
        f"{COMISIONES_URL}?tab={tipo}",
        f"{COMISIONES_URL}?tipo={tipo}",
    ]:
        soup = get_soup(url_alt, session)
        time.sleep(DELAY)
        if soup:
            links = _links_comision_de(soup, tipo, group)
            if links:
                log.info(f"  [{tipo}] encontrado en {url_alt}")
                return links

    log.warning(f"  [{tipo}] Sin resultados — puede requerir Playwright (tabs JS)")
    return []


def _encontrar_contenedor_tab(soup: BeautifulSoup, tipo: str) -> BeautifulSoup | None:
    """Busca el contenedor HTML de una tab específica."""
    # Posibles IDs/atributos
    buscadores = [
        lambda: soup.find(id=tipo),
        lambda: soup.find(id=f"tab-{tipo}"),
        lambda: soup.find(id=f"tab_{tipo}"),
        lambda: soup.find(attrs={"data-tab": tipo}),
        lambda: soup.find(attrs={"data-target": f"#{tipo}"}),
        lambda: _buscar_por_heading(soup, tipo),
    ]
    for fn in buscadores:
        r = fn()
        if r:
            return r
    return None


def _buscar_por_heading(soup: BeautifulSoup, tipo: str):
    """Busca un heading que mencione el tipo y retorna su contenedor."""
    for h in soup.find_all(["h1","h2","h3","h4","h5"]):
        if tipo.lower() in clean(h.get_text()).lower():
            p = h.parent
            if p and len(p.find_all("a")) > 2:
                return p
    return None


def _links_comision_de(soup: BeautifulSoup, tipo: str, group: str) -> list[dict]:
    """Extrae links /actividad-legislativa/comisiones/NNN (sin sub-ruta)."""
    resultado = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = RE_COMISION_PATH.search(href)
        if not m:
            continue
        id_c   = m.group(1)
        id_ses = m.group(2)        # si tiene sesión, es sub-página
        if id_ses:                 # ignorar URLs de sesión directa
            continue
        nombre = clean(a.get_text())
        url    = abs_url(href)
        if not nombre or url in seen:
            continue
        seen.add(url)
        nombre_carpeta = SENADO_PREFIX + nombre
        resultado.append({
            "id_comision":    id_c,
            "nombre":         nombre,
            "tipo":           tipo,
            "group":          group,
            "url":            url,
            "nombre_carpeta": nombre_carpeta,
        })
    return resultado


# ══════════════════════════════════════════════════════════════
# 2. INTEGRANTES
# ══════════════════════════════════════════════════════════════

def scrape_integrantes(comision: dict,
                        session: requests.Session) -> list[dict]:
    """
    Retorna lista de integrantes con formato DataStore:
    [{nombre, cargo, id, url_ficha, chamber}]
    """
    url = comision["url"]
    log.info(f"  Integrantes: {comision['nombre']}")

    soup = get_soup(url, session)
    time.sleep(DELAY)
    if not soup:
        return []

    integrantes = _extraer_integrantes_html(soup)

    # Si no hay, intentar sub-URL /integrantes
    if not integrantes:
        soup2 = get_soup(f"{url}/integrantes", session)
        time.sleep(DELAY)
        if soup2:
            integrantes = _extraer_integrantes_html(soup2)

    log.info(f"    → {len(integrantes)} integrantes")
    return integrantes


def _extraer_integrantes_html(soup: BeautifulSoup) -> list[dict]:
    """
    Estructura observada (imagen 5):
      Cards con foto + 'SENADORA Y PRESIDENTE' + 'Paulina Vodanovic Rojas'
    """
    integrantes = []

    # Método 1: cards con foto y nombre
    for card in soup.select(
        "div.card, div.member-card, article.integrante, "
        "div.integrante, div.senator-card, div[class*='card']"
    ):
        nombre_el = card.select_one(
            "strong, h3, h4, .nombre, .name, p.name, "
            ".card-title, .senator-name"
        )
        cargo_el  = card.select_one(
            ".cargo, .rol, .role, small, .card-subtitle, "
            "span.cargo, span.position"
        )
        link_el   = card.find("a", href=RE_SENADOR)

        nombre = clean(nombre_el.get_text()) if nombre_el else ""
        cargo  = clean(cargo_el.get_text())  if cargo_el  else ""
        href   = link_el["href"]             if link_el   else ""
        m      = RE_SENADOR.search(href)
        id_s   = m.group(1) if m else ""

        if nombre:
            integrantes.append({
                "nombre":    nombre,
                "cargo":     cargo,
                "id":        id_s,
                "url_ficha": abs_url(href),
                "chamber":   "senado",
            })

    if integrantes:
        return _dedup_integrantes(integrantes)

    # Método 2: cualquier link a /senadoras-y-senadores/
    seen = set()
    for a in soup.find_all("a", href=RE_SENADOR):
        href   = a["href"]
        nombre = clean(a.get_text())
        m      = RE_SENADOR.search(href)
        id_s   = m.group(1) if m else ""
        if not nombre or href in seen:
            continue
        seen.add(href)
        cargo = _cargo_cerca(a)
        integrantes.append({
            "nombre":    nombre,
            "cargo":     cargo,
            "id":        id_s,
            "url_ficha": abs_url(href),
            "chamber":   "senado",
        })

    if integrantes:
        return _dedup_integrantes(integrantes)

    # Método 3: tabla de integrantes
    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            cells = row.find_all(["td", "th"])
            if len(cells) < 2:
                continue
            link  = row.find("a", href=RE_SENADOR)
            nombre = clean(cells[0].get_text())
            cargo  = clean(cells[1].get_text()) if len(cells) > 1 else ""
            if nombre.lower() in ["nombre", "integrante", ""]:
                continue
            m    = RE_SENADOR.search(link["href"]) if link else None
            id_s = m.group(1) if m else ""
            integrantes.append({
                "nombre":    nombre,
                "cargo":     cargo,
                "id":        id_s,
                "url_ficha": abs_url(link["href"]) if link else "",
                "chamber":   "senado",
            })

    return _dedup_integrantes(integrantes)


def _cargo_cerca(a_tag) -> str:
    """Infiere el cargo de un senador buscando en elementos cercanos."""
    cargos_kw = ["presidente", "senador", "senadora", "secretari", "vicepresidente"]

    # Elemento hermano previo
    prev = a_tag.find_previous_sibling()
    if prev:
        t = clean(prev.get_text()).lower()
        if any(c in t for c in cargos_kw):
            return clean(prev.get_text())

    # Texto del padre
    if a_tag.parent:
        t = clean(a_tag.parent.get_text()).lower()
        m = re.match(r'\(([^)]+)\)', t)
        if m:
            return m.group(1)
        for word in t.split():
            if any(c in word for c in cargos_kw):
                return word.capitalize()

    return ""


def _dedup_integrantes(items: list[dict]) -> list[dict]:
    seen, out = set(), []
    for i in items:
        k = i["nombre"].lower()
        if k not in seen:
            seen.add(k)
            out.append(i)
    return out


# ══════════════════════════════════════════════════════════════
# 3. SESIONES (lista)
# ══════════════════════════════════════════════════════════════

def scrape_lista_sesiones(comision: dict,
                           session: requests.Session,
                           fecha_desde: str = "",
                           fecha_hasta: str = "",
                           max_paginas: int = 100) -> list[dict]:
    """
    Retorna lista de sesiones en formato historial.csv:
    [{Año, Mes, ID, Fecha, Estado, Citacion, Acta, Cuenta, url_sesion}]
    """
    url_sesiones = f"{comision['url']}/sesiones"
    log.info(f"  Sesiones: {comision['nombre']}  [{fecha_desde}→{fecha_hasta}]")

    rows   = []
    url_act = url_sesiones
    pagina  = 0

    while url_act and pagina < max_paginas:
        pagina += 1
        soup = get_soup(url_act, session)
        time.sleep(DELAY)
        if not soup:
            break

        nuevas = _parsear_lista_sesiones(soup, comision)

        if not nuevas:
            # Intentar también la URL base de comisión directamente
            if pagina == 1:
                soup2 = get_soup(comision["url"], session)
                time.sleep(DELAY)
                if soup2:
                    nuevas = _parsear_lista_sesiones(soup2, comision)
            if not nuevas:
                break

        parar = False
        for s in nuevas:
            fi = s.get("fecha_iso", "")
            if fecha_desde or fecha_hasta:
                if fecha_en_rango(fi, fecha_desde, fecha_hasta):
                    rows.append(s)
                elif fi and fecha_desde and fi < fecha_desde:
                    parar = True   # estamos viendo fechas más antiguas
            else:
                rows.append(s)

        if parar:
            break

        url_act = next_page_url(soup, url_act)
        log.info(f"    pág {pagina}: {len(nuevas)} sesiones, total: {len(rows)}")

    log.info(f"  → {len(rows)} sesiones encontradas")
    return rows


def _parsear_lista_sesiones(soup: BeautifulSoup, comision: dict) -> list[dict]:
    """
    Extrae links de sesión del HTML.
    URL patrón: /actividad-legislativa/comisiones/{id_com}/{id_ses}
    """
    sesiones = []
    seen = set()

    for a in soup.find_all("a", href=RE_SESION):
        href = a["href"]
        if href in seen:
            continue
        seen.add(href)

        m_ses = RE_SESION.search(href)
        id_s  = m_ses.group(1) if m_ses else href.split("/")[-1]

        # Fecha y estado del texto o contexto
        ctx_texto = clean(a.get_text())
        if not ctx_texto and a.parent:
            ctx_texto = clean(a.parent.get_text())

        anio, mes, fecha_iso, fecha_texto = parse_fecha_es(ctx_texto)

        # Horarios
        horas = RE_HORA.findall(ctx_texto)
        hora_i = horas[0] if horas        else ""
        hora_t = horas[1] if len(horas)>1 else ""

        # Estado (buscar en contexto cercano)
        estado = _extraer_estado(a)

        sesiones.append({
            # Campos DataStore (historial.csv)
            "Año":      anio,
            "Mes":      mes,
            "ID":       id_s,
            "Fecha":    fecha_texto or fecha_iso,
            "Estado":   estado,
            "Citacion": "",
            "Acta":     "",
            "Cuenta":   "",
            # Campos extra para enriquecer
            "fecha_iso":      fecha_iso,
            "hora_inicio":    hora_i,
            "hora_termino":   hora_t,
            "url_sesion":     abs_url(href),
            "id_comision":    comision.get("id_comision", ""),
        })

    return sesiones


def _extraer_estado(a_tag) -> str:
    """Busca el estado de una sesión en elementos cercanos al link."""
    estados_kw = ["CITADA","CELEBRADA","SUSPENDIDA","FRACASADA","REALIZADA"]
    # Revisar texto del padre y abuelo
    for el in [a_tag.parent, a_tag.parent.parent if a_tag.parent else None]:
        if not el:
            continue
        texto = clean(el.get_text()).upper()
        for est in estados_kw:
            if est in texto:
                return est
    return ""


# ══════════════════════════════════════════════════════════════
# 4. DETALLE DE SESIÓN  (puntos + presentaciones)
# ══════════════════════════════════════════════════════════════

def scrape_detalle_sesion(sesion: dict,
                           session: requests.Session) -> dict:
    """
    Visita la página de sesión y extrae:
    - lugar, hora_inicio, hora_termino (refinados)
    - puntos (boletín, tema, aspectos, acuerdos, invitados)
    - presentaciones (título, organización, url_pdf)
    - Citacion / Acta / Cuenta (URLs de documentos)
    - texto_completo para el .txt del transcript

    Retorna dict con claves que enriquecen la sesion.
    """
    url = sesion.get("url_sesion","")
    if not url:
        return sesion

    log.info(f"    Sesión {sesion.get('ID','')} — {url}")
    soup = get_soup(url, session)
    time.sleep(DELAY)
    if not soup:
        return sesion

    extra = {
        "lugar":             "",
        "hora_inicio":       sesion.get("hora_inicio",""),
        "hora_termino":      sesion.get("hora_termino",""),
        "Citacion":          sesion.get("Citacion",""),
        "Acta":              sesion.get("Acta",""),
        "Cuenta":            sesion.get("Cuenta",""),
        "puntos":            [],
        "presentaciones":    [],
        "integrantes_texto": "",
        "texto_completo":    "",
    }

    # ── Datos generales ──────────────────────────────────────
    _extraer_datos_generales(soup, extra)

    # ── Puntos de la sesión ──────────────────────────────────
    puntos = _extraer_puntos(soup, sesion)
    extra["puntos"] = puntos

    # ── Presentaciones (tab separada) ────────────────────────
    pres = _extraer_presentaciones(soup, url, session)
    extra["presentaciones"] = pres

    # ── Integrantes presentes ────────────────────────────────
    extra["integrantes_texto"] = _extraer_integrantes_presentes_texto(soup)

    # ── Construir texto para .txt (transcript) ───────────────
    extra["texto_completo"] = _construir_transcript(sesion, extra)

    return {**sesion, **extra}


def _extraer_datos_generales(soup: BeautifulSoup, dest: dict):
    """Extrae Inicio, Término, Lugar de la ficha de sesión."""
    # Buscar tabla o ficha con datos
    for row in soup.select("table tr, dl, div.dato, div.info-row"):
        cells = row.find_all(["td","th","dt","dd","label","span"])
        if len(cells) < 2:
            continue
        label = clean(cells[0].get_text()).lower().rstrip(":")
        valor = clean(cells[1].get_text())
        if "inicio"  in label: dest["hora_inicio"]  = valor
        if "término" in label or "termino" in label:
                                dest["hora_termino"] = valor
        if "lugar"   in label: dest["lugar"]        = valor

    # Buscar links de Citación / Acta / Cuenta
    for a in soup.find_all("a", href=True):
        texto = clean(a.get_text()).lower()
        href  = abs_url(a["href"])
        if "citaci" in texto and not dest["Citacion"]:
            dest["Citacion"] = href
        elif "acta" in texto and not dest["Acta"]:
            dest["Acta"] = href
        elif "cuenta" in texto and not dest["Cuenta"]:
            dest["Cuenta"] = href


def _extraer_puntos(soup: BeautifulSoup, sesion: dict) -> list[dict]:
    """
    Extrae puntos de sesión.
    Estructura observada (imagen 7):
      Punto 1
      Boletín: 14594-06 ↗
      Tema: Modifica distintos cuerpos legales…
      Aspectos considerados: Se continuó…
      Acuerdos: Continuar en una próxima sesión.
    """
    puntos = []

    # Buscar nodos "Punto N"
    punto_nodos = soup.find_all(string=re.compile(r'^\s*[Pp]unto\s+\d+\s*$'))

    if not punto_nodos:
        # Buscar por contenedor con clase/atributo
        containers = soup.select(
            "div.punto, section.punto, div.agenda-item, "
            "div[class*='punto'], li.point"
        )
        if containers:
            for i, c in enumerate(containers, 1):
                puntos.append(_parsear_bloque_punto(c, i, sesion))
            return puntos

    for i, nodo in enumerate(punto_nodos, 1):
        contenedor = nodo.parent
        if contenedor:
            puntos.append(_parsear_bloque_punto(contenedor, i, sesion))

    # Si no encontramos, buscar anclados en "Boletín:"
    if not puntos:
        for i, b_nodo in enumerate(soup.find_all(string=re.compile(r'Boletín')), 1):
            c = b_nodo.parent
            if c:
                puntos.append(_parsear_bloque_punto(c, i, sesion))

    return puntos


def _parsear_bloque_punto(contenedor, numero: int, sesion: dict) -> dict:
    punto = {
        "numero":                numero,
        "id_sesion":             sesion.get("ID",""),
        "boletin":               "",
        "url_boletin":           "",
        "tema":                  "",
        "aspectos_considerados": "",
        "acuerdos":              "",
        "invitados":             "",
    }

    texto = clean(contenedor.get_text())

    # Boletín por regex
    m = RE_BOLETIN.search(texto)
    if m:
        punto["boletin"] = m.group(1)

    # Link al boletín
    for a in contenedor.find_all("a"):
        href = a.get("href","")
        if RE_BOLETIN.search(href) or "boletin" in href.lower():
            punto["url_boletin"] = abs_url(href)
            if not punto["boletin"]:
                punto["boletin"] = clean(a.get_text())
            break

    # Extraer campos por keyword
    campos = [
        ("tema",                  ["Tema:","TEMA:"]),
        ("aspectos_considerados", ["Aspectos considerados:","Aspectos:"]),
        ("acuerdos",              ["Acuerdos:","ACUERDOS:"]),
        ("invitados",             ["Invitados:","INVITADOS:"]),
    ]
    for campo, kws in campos:
        for kw in kws:
            idx = texto.find(kw)
            if idx >= 0:
                ini = idx + len(kw)
                fin = len(texto)
                for _, otros_kws in campos:
                    for kw2 in otros_kws:
                        p = texto.find(kw2, ini)
                        if 0 < p < fin:
                            fin = p
                for stop in ["Integrantes:", "Punto ", "Invitados:"]:
                    p = texto.find(stop, ini)
                    if 0 < p < fin:
                        fin = p
                punto[campo] = texto[ini:fin].strip()
                break

    # Refinamiento por HTML (strong/b labels)
    for strong in contenedor.find_all(["strong","b","dt"]):
        label = clean(strong.get_text()).lower().rstrip(":")
        val_el = strong.find_next_sibling(["p","dd","span","div"])
        valor = clean(val_el.get_text()) if val_el else ""

        if "tema" in label and not punto["tema"]:
            punto["tema"] = valor
        elif "aspecto" in label and not punto["aspectos_considerados"]:
            punto["aspectos_considerados"] = valor
        elif "acuerdo" in label and not punto["acuerdos"]:
            punto["acuerdos"] = valor
        elif "invitado" in label and not punto["invitados"]:
            punto["invitados"] = valor

    return punto


def _extraer_presentaciones(soup: BeautifulSoup,
                              url_sesion: str,
                              session: requests.Session) -> list[dict]:
    """
    Extrae la tabla de presentaciones (imagen 8).
    Estructura: TÍTULO | ORGANIZACIÓN | DOCUMENTO (PDF)
    """
    pres = []
    _pres_de_soup(soup, pres)

    # Si hay tab de presentaciones, visitarla
    if not pres:
        for a in soup.find_all("a"):
            txt = clean(a.get_text()).lower()
            href = a.get("href","")
            if "presentaci" in txt and href:
                url_tab = abs_url(href)
                if url_tab != url_sesion:
                    soup2 = get_soup(url_tab, session)
                    time.sleep(DELAY)
                    if soup2:
                        _pres_de_soup(soup2, pres)
                    break
    return pres


def _pres_de_soup(soup: BeautifulSoup, dest: list):
    """Lee tabla de presentaciones del HTML."""
    for table in soup.find_all("table"):
        headers = [clean(th.get_text()).upper()
                   for th in table.select("th")]
        if not any(k in " ".join(headers)
                   for k in ["TÍTULO","TITULO","ORGANIZACIÓN","DOCUMENTO","PRESENTACION"]):
            # Intento por contenido
            txt_tabla = clean(table.get_text()).lower()
            if not any(k in txt_tabla for k in ["organización","senado.","comisión de"]):
                continue

        for row in table.find_all("tr"):
            cells = row.find_all("td")
            if not cells:
                continue
            titulo = clean(cells[0].get_text())
            org    = clean(cells[1].get_text()) if len(cells) > 1 else ""
            a_pdf  = (cells[2].find("a") if len(cells) > 2
                      else row.find("a", href=re.compile(r'\.pdf', re.I)))
            url_pdf = abs_url(a_pdf.get("href","")) if a_pdf else ""

            if titulo:
                dest.append({
                    "titulo":        titulo,
                    "organizacion":  org,
                    "url_documento": url_pdf,
                })

    # PDFs sueltos si no hay tabla
    if not dest:
        for a in soup.find_all("a", href=re.compile(r'\.pdf', re.I)):
            txt = clean(a.get_text())
            dest.append({
                "titulo":        txt or a["href"].split("/")[-1],
                "organizacion":  "",
                "url_documento": abs_url(a["href"]),
            })


def _extraer_integrantes_presentes_texto(soup: BeautifulSoup) -> str:
    """Extrae el texto de la sección 'Integrantes:' de la sesión."""
    int_nodo = soup.find(string=re.compile(r'Integrantes:'))
    if not int_nodo or not int_nodo.parent:
        return ""
    lines = []
    for sib in int_nodo.parent.find_next_siblings(["p","li","div","span"]):
        txt = clean(sib.get_text())
        if any(kw in txt for kw in ["Invitados:","Presentaciones:","Punto ","Boletín"]):
            break
        if txt and len(txt) > 4:
            lines.append(txt)
    return "; ".join(lines)


def _construir_transcript(sesion: dict, extra: dict) -> str:
    """
    Construye el texto del .txt para la carpeta transcripts/.
    Sirve para el agente RAG (DataStore.search_texts).
    """
    lines = []
    lines.append(f"SESIÓN {sesion.get('ID','')} — {sesion.get('Fecha','')}")
    lines.append(f"COMISIÓN: {sesion.get('nombre_comision','')}")
    lines.append(f"ESTADO: {sesion.get('Estado','')}")
    lines.append(f"LUGAR: {extra.get('lugar','')}")
    lines.append(f"HORA: {extra.get('hora_inicio','')} – {extra.get('hora_termino','')}")
    if extra.get("Citacion"):
        lines.append(f"CITACIÓN: {extra['Citacion']}")
    if extra.get("Acta"):
        lines.append(f"ACTA: {extra['Acta']}")
    lines.append("")

    for p in extra.get("puntos", []):
        lines.append(f"PUNTO {p['numero']}")
        if p.get("boletin"):
            lines.append(f"Boletín: {p['boletin']}")
        if p.get("tema"):
            lines.append(f"Tema: {p['tema']}")
        if p.get("aspectos_considerados"):
            lines.append(f"Aspectos considerados: {p['aspectos_considerados']}")
        if p.get("acuerdos"):
            lines.append(f"Acuerdos: {p['acuerdos']}")
        if p.get("invitados"):
            lines.append(f"Invitados: {p['invitados']}")
        lines.append("")

    if extra.get("presentaciones"):
        lines.append("PRESENTACIONES ANTE COMISIÓN:")
        for pr in extra["presentaciones"]:
            lines.append(f"- {pr.get('titulo','')} [{pr.get('organizacion','')}]")
            if pr.get("url_documento"):
                lines.append(f"  PDF: {pr['url_documento']}")
        lines.append("")

    if extra.get("integrantes_texto"):
        lines.append(f"INTEGRANTES PRESENTES: {extra['integrantes_texto']}")

    return "\n".join(lines)
