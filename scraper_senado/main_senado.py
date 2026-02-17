"""
main_senado.py
==============
Orquestador del scraper del Senado de Chile.
Escribe directamente a la estructura del DataStore (REPO_V40_HISTORIAL_COMPLETO_V2).

ESTRUCTURA DE SALIDA (compatible con DataStore existente):
  REPO_V40_HISTORIAL_COMPLETO_V2/
    Permanentes/
      Senado_Comision de Gobierno, Descentralizacion y Regionalizacion/
        historial.csv          ← Año, Mes, ID, Fecha, Estado, Citacion, Acta, Cuenta
        integrantes.json       ← {integrantes:[{nombre,cargo,id,url_ficha,chamber}]}
        sesiones_meta.json     ← metadata extra (puntos, presentaciones, lugar)
        transcripts/
          {id_sesion}.txt      ← texto para agente RAG
    Otras/
      Senado_Comision de Medio Ambiente/  ...
    Unidas/
      Senado_Comision Unida Educacion-Salud/ ...

USO:
  # Todo (todas las comisiones, sin filtro de fecha)
  python main_senado.py --repo ../REPO_V40_HISTORIAL_COMPLETO_V2

  # Solo permanentes, 2025 en adelante
  python main_senado.py --repo ../REPO_V40_HISTORIAL_COMPLETO_V2 \\
         --tipos permanentes --desde 2025-01-01

  # Solo listado + integrantes (sin sesiones, rápido)
  python main_senado.py --repo ../REPO_V40_HISTORIAL_COMPLETO_V2 --solo-listado

  # Reanudar scraping interrumpido
  python main_senado.py --repo ../REPO_V40_HISTORIAL_COMPLETO_V2 --reanudar

  # Modo sin detalles de sesión (solo historial, mucho más rápido)
  python main_senado.py --repo ../REPO_V40_HISTORIAL_COMPLETO_V2 --sin-detalle-sesion
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime

from config_senado import TIPO_A_GROUP, DELAY
from utils_senado import (
    make_session, write_historial_csv, write_integrantes_json,
    write_json, write_txt, read_json, get_soup
)
from scraper_senado import (
    scrape_listado_comisiones,
    scrape_integrantes,
    scrape_lista_sesiones,
    scrape_detalle_sesion,
)


# ── Logging ──────────────────────────────────────────────────
def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("senado_scraper.log", encoding="utf-8"),
        ],
        force=True,
    )

log = logging.getLogger(__name__)


# ── Checkpoint ───────────────────────────────────────────────
def cp_path(repo: str) -> str:
    return os.path.join(repo, "_senado_checkpoint.json")


def guardar_cp(repo: str, data: dict):
    with open(cp_path(repo), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def cargar_cp(repo: str) -> dict:
    p = cp_path(repo)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {}


# ══════════════════════════════════════════════════════════════
# PASO 1 — Listado de comisiones
# ══════════════════════════════════════════════════════════════
def paso1_listado(session, tipos: list[str],
                  repo: str, reanudar: bool) -> list[dict]:
    cp = cargar_cp(repo) if reanudar else {}
    if "comisiones" in cp:
        log.info(f"[CP] Usando {len(cp['comisiones'])} comisiones del checkpoint.")
        return cp["comisiones"]

    comisiones = scrape_listado_comisiones(session, tipos)

    if not comisiones:
        log.error("Sin comisiones. Posible problema con tabs JS — usando Playwright...")
        comisiones = _fallback_playwright(tipos)

    if comisiones:
        cp["comisiones"] = comisiones
        guardar_cp(repo, cp)

    return comisiones


def _fallback_playwright(tipos: list[str]) -> list[dict]:
    """Intenta obtener listado usando Playwright si requests puro falla."""
    try:
        from selenium_fallback_senado import obtener_listado_con_browser
        return obtener_listado_con_browser(tipos)
    except ImportError:
        log.warning("selenium_fallback_senado.py no disponible.")
        return []


# ══════════════════════════════════════════════════════════════
# PASO 2 — Por cada comisión: integrantes + sesiones + detalle
# ══════════════════════════════════════════════════════════════
def paso2_procesar_comision(comision: dict,
                              session,
                              repo: str,
                              fecha_desde: str,
                              fecha_hasta: str,
                              con_detalle: bool,
                              cp: dict) -> bool:
    """
    Procesa una comisión completa y escribe los archivos al REPO.
    Retorna True si completó (o ya estaba), False si falló.
    """
    id_c     = comision["id_comision"]
    nombre   = comision["nombre"]
    group    = comision["group"]
    nom_carp = comision["nombre_carpeta"]

    # Ruta de la carpeta de esta comisión en el REPO
    commission_dir = os.path.join(repo, group, nom_carp)

    # Verificar si ya fue procesada (checkpoint)
    ya_procesadas = set(cp.get("procesadas", []))
    if id_c in ya_procesadas:
        log.info(f"  [CP] Ya procesada: {nombre}")
        return True

    log.info(f"\n{'─'*55}")
    log.info(f"  Comisión: {nombre}  ({comision['tipo']})")
    log.info(f"  Carpeta:  {commission_dir}")

    os.makedirs(commission_dir, exist_ok=True)

    # ── Integrantes ──────────────────────────────────────────
    integrantes = scrape_integrantes(comision, session)
    int_path = os.path.join(commission_dir, "integrantes.json")
    write_integrantes_json(int_path, integrantes)

    # ── Sesiones (lista) ─────────────────────────────────────
    sesiones_raw = scrape_lista_sesiones(
        comision, session, fecha_desde, fecha_hasta
    )

    if not sesiones_raw:
        log.warning(f"  Sin sesiones encontradas para {nombre}")
        # Crear historial vacío
        hist_path = os.path.join(commission_dir, "historial.csv")
        write_historial_csv(hist_path, [])
        _marcar_procesada(cp, id_c, repo)
        return True

    # Agregar nombre de comisión a cada sesión (para transcript)
    for s in sesiones_raw:
        s["nombre_comision"] = nombre

    # ── Detalle de sesiones (puntos + presentaciones) ────────
    sesiones_detalle = []
    historial_rows   = []
    transcripts_dir  = os.path.join(commission_dir, "transcripts")

    for i, ses in enumerate(sesiones_raw, 1):
        sid = ses.get("ID","")

        if con_detalle:
            log.info(f"    [{i}/{len(sesiones_raw)}] Sesión {sid}")
            det = scrape_detalle_sesion(ses, session)
        else:
            det = ses   # sin detalle

        sesiones_detalle.append(det)

        # Fila para historial.csv
        historial_rows.append({
            "Año":      det.get("Año",""),
            "Mes":      det.get("Mes",""),
            "ID":       sid,
            "Fecha":    det.get("Fecha",""),
            "Estado":   det.get("Estado",""),
            "Citacion": det.get("Citacion",""),
            "Acta":     det.get("Acta",""),
            "Cuenta":   det.get("Cuenta",""),
        })

        # Transcript .txt para agente RAG
        if con_detalle and det.get("texto_completo") and sid:
            os.makedirs(transcripts_dir, exist_ok=True)
            txt_path = os.path.join(transcripts_dir, f"{sid}.txt")
            write_txt(txt_path, det["texto_completo"])

    # ── Escribir historial.csv ───────────────────────────────
    hist_path = os.path.join(commission_dir, "historial.csv")
    write_historial_csv(hist_path, historial_rows)

    # ── Escribir sesiones_meta.json (metadata extra) ─────────
    # Solo guardamos lo útil (sin texto_completo que ya está en .txt)
    meta_sesiones = []
    for d in sesiones_detalle:
        meta = {k: v for k, v in d.items()
                if k not in ("texto_completo",)}
        meta_sesiones.append(meta)

    meta_path = os.path.join(commission_dir, "sesiones_meta.json")
    write_json(meta_path, {
        "comision":      nombre,
        "id_comision":   id_c,
        "group":         group,
        "total":         len(meta_sesiones),
        "generado":      datetime.now().isoformat(),
        "sesiones":      meta_sesiones,
    })

    _marcar_procesada(cp, id_c, repo)
    log.info(f"  ✓ {nombre}: {len(historial_rows)} sesiones, "
             f"{len(integrantes)} integrantes")
    return True


def _marcar_procesada(cp: dict, id_c: str, repo: str):
    procesadas = set(cp.get("procesadas", []))
    procesadas.add(id_c)
    cp["procesadas"] = list(procesadas)
    guardar_cp(repo, cp)


# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(
        description="Scraper Senado de Chile → compatible con DataStore OBSERVATORIO-POLITICO"
    )
    parser.add_argument(
        "--repo", required=True,
        help="Ruta a REPO_V40_HISTORIAL_COMPLETO_V2 (o cualquier repo DataStore)"
    )
    parser.add_argument(
        "--tipos", nargs="+",
        choices=list(TIPO_A_GROUP.keys()) + ["todas"],
        default=["todas"],
    )
    parser.add_argument("--desde", default="",
                        help="Fecha inicio sesiones (YYYY-MM-DD)")
    parser.add_argument("--hasta", default="",
                        help="Fecha fin sesiones (YYYY-MM-DD, default: hoy)")
    parser.add_argument("--solo-listado", action="store_true",
                        help="Solo obtener comisiones e integrantes (sin sesiones)")
    parser.add_argument("--sin-detalle-sesion", action="store_true",
                        help="No scrappear detalle de cada sesión (más rápido)")
    parser.add_argument("--reanudar", action="store_true",
                        help="Continuar desde el checkpoint guardado")
    parser.add_argument("--verbose", "-v", action="store_true")

    args = parser.parse_args()
    setup_logging(args.verbose)

    # Validar repo
    if not os.path.isdir(args.repo):
        log.error(f"Directorio REPO no existe: {args.repo}")
        sys.exit(1)

    tipos = (list(TIPO_A_GROUP.keys()) if "todas" in args.tipos else args.tipos)
    hasta = args.hasta or datetime.today().strftime("%Y-%m-%d")
    con_detalle = not args.sin_detalle_sesion and not args.solo_listado

    log.info("=" * 60)
    log.info("  SCRAPER SENADO DE CHILE")
    log.info(f"  REPO:        {args.repo}")
    log.info(f"  Tipos:       {tipos}")
    log.info(f"  Rango:       {args.desde or 'sin inicio'} → {hasta}")
    log.info(f"  Con detalle: {con_detalle}")
    log.info(f"  Reanudar:    {args.reanudar}")
    log.info("=" * 60)

    session = make_session()
    ts = datetime.now()
    cp = cargar_cp(args.repo) if args.reanudar else {}

    # ── PASO 1 ───────────────────────────────────────────────
    log.info("\n[1/2] Obteniendo listado de comisiones...")
    comisiones = paso1_listado(session, tipos, args.repo, args.reanudar)

    if not comisiones:
        log.error("No se encontraron comisiones. "
                  "Verifica conexión o usa --verbose para debug.")
        sys.exit(1)

    # Resumen por tipo
    for tipo in tipos:
        n = sum(1 for c in comisiones if c["tipo"] == tipo)
        log.info(f"  {tipo:15s}: {n} comisiones")

    if args.solo_listado:
        # Solo guardar listado como JSON de referencia
        listado_path = os.path.join(args.repo, "_senado_listado.json")
        write_json(listado_path, {"comisiones": comisiones, "generado": ts.isoformat()})
        log.info(f"Listado guardado en {listado_path}")

        # Pero sí obtener integrantes
        log.info("\nObteniendo integrantes...")
        for i, com in enumerate(comisiones, 1):
            log.info(f"[{i}/{len(comisiones)}] {com['nombre']}")
            commission_dir = os.path.join(args.repo, com["group"], com["nombre_carpeta"])
            os.makedirs(commission_dir, exist_ok=True)
            integrantes = scrape_integrantes(com, session)
            int_path = os.path.join(commission_dir, "integrantes.json")
            write_integrantes_json(int_path, integrantes)
            time.sleep(DELAY)
        return

    # ── PASO 2 ───────────────────────────────────────────────
    log.info(f"\n[2/2] Procesando {len(comisiones)} comisiones...")

    ok, fail = 0, 0
    for i, com in enumerate(comisiones, 1):
        log.info(f"\n[{i}/{len(comisiones)}]")
        try:
            res = paso2_procesar_comision(
                com, session, args.repo,
                args.desde, hasta,
                con_detalle, cp
            )
            if res:
                ok += 1
            else:
                fail += 1
        except KeyboardInterrupt:
            log.warning("Interrumpido por usuario. Progreso guardado.")
            break
        except Exception as e:
            log.error(f"  Error en {com['nombre']}: {e}")
            fail += 1
        time.sleep(DELAY)

    # ── Resumen ──────────────────────────────────────────────
    duracion = datetime.now() - ts
    log.info("\n" + "=" * 60)
    log.info("  COMPLETADO")
    log.info(f"  Comisiones OK:     {ok}")
    log.info(f"  Comisiones error:  {fail}")
    log.info(f"  Tiempo total:      {duracion}")
    log.info(f"  REPO:              {args.repo}")
    log.info("=" * 60)
    log.info("\nAhora puedes reiniciar el servidor FastAPI y las comisiones")
    log.info("del Senado aparecerán en /comisiones.html (group=Permanentes/Otras/Unidas)")


if __name__ == "__main__":
    main()
