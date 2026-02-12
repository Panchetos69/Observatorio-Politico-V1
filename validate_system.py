#!/usr/bin/env python3
"""
Script de validación completa del sistema Observatorio Político
Verifica configuración, archivos y estructura
"""
import os
import sys
import json
import glob
from pathlib import Path
from dotenv import load_dotenv


PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(PROJECT_DIR, ".env"))

class bcolors:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKCYAN = '\033[96m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'


def print_section(title):
    print(f"\n{bcolors.HEADER}{bcolors.BOLD}{'='*60}{bcolors.ENDC}")
    print(f"{bcolors.HEADER}{bcolors.BOLD}{title}{bcolors.ENDC}")
    print(f"{bcolors.HEADER}{bcolors.BOLD}{'='*60}{bcolors.ENDC}\n")


def print_success(msg):
    print(f"{bcolors.OKGREEN}✓ {msg}{bcolors.ENDC}")


def print_error(msg):
    print(f"{bcolors.FAIL}✗ {msg}{bcolors.ENDC}")


def print_warning(msg):
    print(f"{bcolors.WARNING}⚠ {msg}{bcolors.ENDC}")


def print_info(msg):
    print(f"{bcolors.OKBLUE}ℹ {msg}{bcolors.ENDC}")


def check_env_variables():
    """Verifica las variables de entorno"""
    print_section("1. VARIABLES DE ENTORNO")
    
    required_vars = {
        'DATA_REPO_DIR': 'Directorio con datos del repositorio',
        'KOM_DIR': 'Directorio de perfiles KOM',
        'GEMINI_API_KEY': 'API Key de Gemini (para chat IA)'
    }
    
    all_ok = True
    env_values = {}
    
    for var, description in required_vars.items():
        value = os.getenv(var)
        env_values[var] = value
        
        if value:
            print_success(f"{var} está configurado")
            print_info(f"  Valor: {value[:50]}{'...' if len(value) > 50 else ''}")
        else:
            print_error(f"{var} no está configurado")
            print_info(f"  Descripción: {description}")
            all_ok = False
    
    if not all_ok:
        print_warning("\n⚠️  Algunas variables no están configuradas")
        print_info("  Crea un archivo .env en la raíz del proyecto:")
        print_info("  cp .env.example .env")
        print_info("  nano .env")
    
    return env_values


def check_directories(env_values):
    """Verifica que los directorios existan"""
    print_section("2. ESTRUCTURA DE DIRECTORIOS")
    
    all_ok = True
    
    # Directorio del proyecto
    project_dir = os.getcwd()
    print_info(f"Directorio del proyecto: {project_dir}")
    
    # Verificar directorios de variables de entorno
    dirs_to_check = {
        'DATA_REPO_DIR': env_values.get('DATA_REPO_DIR'),
        'KOM_DIR': env_values.get('KOM_DIR'),
    }
    
    for name, path in dirs_to_check.items():
        if not path:
            continue
            
        abs_path = os.path.abspath(path)
        if os.path.isdir(abs_path):
            print_success(f"{name}: {abs_path}")
        else:
            print_error(f"{name}: {abs_path} (NO EXISTE)")
            all_ok = False
    
    # Verificar DIARIO_OFICIAL_EXPORT
    kom_dir = env_values.get('KOM_DIR')
    if kom_dir:
        project_dir_from_kom = os.path.abspath(os.path.join(kom_dir, ".."))
        diario_dir = os.path.join(project_dir_from_kom, "DIARIO_OFICIAL_EXPORT")
        
        print_info(f"\nVerificando DIARIO_OFICIAL_EXPORT:")
        print_info(f"  KOM_DIR: {kom_dir}")
        print_info(f"  Project dir: {project_dir_from_kom}")
        print_info(f"  DIARIO_OFICIAL_EXPORT: {diario_dir}")
        
        if os.path.isdir(diario_dir):
            print_success("DIARIO_OFICIAL_EXPORT existe")
            return diario_dir, True
        else:
            print_error("DIARIO_OFICIAL_EXPORT NO EXISTE")
            print_warning(f"  Crear el directorio: mkdir -p '{diario_dir}'")
            return diario_dir, False
    
    return None, all_ok


def check_news_files(diario_dir):
    """Verifica archivos de noticias"""
    print_section("3. ARCHIVOS DE NOTICIAS")
    
    if not diario_dir or not os.path.isdir(diario_dir):
        print_error("No se puede verificar (directorio no existe)")
        return False
    
    # Listar todos los archivos
    all_files = os.listdir(diario_dir)
    print_info(f"Archivos encontrados: {len(all_files)}")
    
    if all_files:
        print("\nListado completo:")
        for f in sorted(all_files):
            fpath = os.path.join(diario_dir, f)
            size = os.path.getsize(fpath)
            print(f"  - {f} ({size:,} bytes)")
    
    # Buscar archivos JSON (sin "log")
    json_files = [
        f for f in all_files
        if f.endswith('.json') and 'log' not in f.lower()
    ]
    
    print(f"\n{bcolors.BOLD}Archivos JSON válidos (sin 'log'): {len(json_files)}{bcolors.ENDC}")
    if json_files:
        for jf in json_files:
            print_success(jf)
    else:
        print_warning("No se encontraron archivos JSON válidos")
    
    # Buscar archivos CSV
    csv_files = [f for f in all_files if f.endswith('.csv')]
    
    print(f"\n{bcolors.BOLD}Archivos CSV: {len(csv_files)}{bcolors.ENDC}")
    if csv_files:
        for cf in csv_files:
            print_success(cf)
    else:
        print_warning("No se encontraron archivos CSV")
    
    if not json_files and not csv_files:
        print_error("\n❌ No hay archivos de noticias disponibles")
        print_info("  Agrega archivos .json o .csv al directorio:")
        print_info(f"  {diario_dir}")
        return False
    
    # Analizar el archivo más reciente
    candidates = []
    for f in json_files:
        candidates.append(os.path.join(diario_dir, f))
    for f in csv_files:
        candidates.append(os.path.join(diario_dir, f))
    
    if not candidates:
        return False
    
    latest_file = max(candidates, key=lambda p: os.path.getmtime(p))
    
    print(f"\n{bcolors.BOLD}Archivo más reciente:{bcolors.ENDC}")
    print_info(f"  {os.path.basename(latest_file)}")
    
    return analyze_file(latest_file)


def analyze_file(filepath):
    """Analiza el contenido de un archivo de noticias"""
    print_section("4. ANÁLISIS DEL ARCHIVO")
    
    print_info(f"Analizando: {os.path.basename(filepath)}")
    print_info(f"Tamaño: {os.path.getsize(filepath):,} bytes")
    
    items = []
    
    if filepath.endswith('.json'):
        print_info("Formato: JSON")
        
        try:
            with open(filepath, 'r', encoding='utf-8-sig', errors='ignore') as f:
                content = f.read().strip()
            
            if content.startswith('['):
                print_info("Tipo: JSON Array")
                data = json.loads(content)
                if isinstance(data, list):
                    items = [x for x in data if isinstance(x, dict)]
            else:
                print_info("Tipo: JSONL (línea por línea)")
                for i, line in enumerate(content.splitlines(), 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        if isinstance(obj, dict):
                            items.append(obj)
                    except Exception as e:
                        print_warning(f"  Error en línea {i}: {str(e)[:50]}")
            
            print_success(f"Parseado correctamente: {len(items)} items")
            
        except Exception as e:
            print_error(f"Error parseando JSON: {e}")
            return False
    
    elif filepath.endswith('.csv'):
        print_info("Formato: CSV")
        
        try:
            import csv
            with open(filepath, 'r', encoding='utf-8-sig', errors='ignore', newline='') as f:
                reader = csv.DictReader(f)
                items = list(reader)
            
            print_success(f"Parseado correctamente: {len(items)} items")
            
        except Exception as e:
            print_error(f"Error parseando CSV: {e}")
            return False
    
    if not items:
        print_error("No se encontraron items en el archivo")
        return False
    
    # Analizar campos
    print(f"\n{bcolors.BOLD}Muestra de datos (primeros 3 items):{bcolors.ENDC}")
    
    for i, item in enumerate(items[:3], 1):
        print(f"\n{bcolors.OKCYAN}Item #{i}:{bcolors.ENDC}")
        for key in list(item.keys())[:8]:
            value = str(item.get(key, ""))[:80]
            print(f"  {key}: {value}")
    
    # Campos disponibles
    all_keys = set()
    for item in items:
        all_keys.update(item.keys())
    
    print(f"\n{bcolors.BOLD}Campos disponibles en los datos:{bcolors.ENDC}")
    print(f"  {', '.join(sorted(all_keys))}")
    
    # Verificar campos requeridos
    print(f"\n{bcolors.BOLD}Verificación de campos:{bcolors.ENDC}")
    
    has_title = any(k in all_keys for k in ['titulo', 'title', 'Título'])
    has_date = any(k in all_keys for k in ['fecha', 'date', 'Fecha'])
    has_url = any(k in all_keys for k in ['pdf_url', 'url', 'link'])
    
    if has_title:
        print_success("Tiene campo de título (titulo/title)")
    else:
        print_error("No tiene campo de título")
    
    if has_date:
        print_success("Tiene campo de fecha (fecha/date)")
    else:
        print_error("No tiene campo de fecha")
    
    if has_url:
        print_success("Tiene campo de URL (pdf_url/url/link)")
    else:
        print_warning("No tiene campo de URL (opcional)")
    
    return has_title and has_date


def check_api_structure():
    """Verifica la estructura de archivos del API"""
    print_section("5. ESTRUCTURA DEL API")
    
    required_files = {
        'api/__init__.py': 'Paquete API',
        'api/index.py': 'Punto de entrada principal',
        'api/datastore.py': 'Gestión de datos',
        'api/agent.py': 'Agente de IA',
    }
    
    all_ok = True
    
    for file_path, description in required_files.items():
        if os.path.exists(file_path):
            print_success(f"{file_path} - {description}")
        else:
            print_error(f"{file_path} - {description} (NO ENCONTRADO)")
            all_ok = False
    
    return all_ok


def main():
    print(f"\n{bcolors.HEADER}{bcolors.BOLD}")
    print("╔═══════════════════════════════════════════════════════════╗")
    print("║  VALIDACIÓN DEL SISTEMA - OBSERVATORIO POLÍTICO          ║")
    print("╚═══════════════════════════════════════════════════════════╝")
    print(f"{bcolors.ENDC}\n")
    
    results = {}
    
    # 1. Variables de entorno
    env_values = check_env_variables()
    results['env'] = all(env_values.values())
    
    # 2. Directorios
    diario_dir, dirs_ok = check_directories(env_values)
    results['dirs'] = dirs_ok
    
    # 3. Archivos de noticias
    files_ok = check_news_files(diario_dir)
    results['files'] = files_ok
    
    # 4. Estructura del API
    api_ok = check_api_structure()
    results['api'] = api_ok
    
    # Resumen final
    print_section("RESUMEN")
    
    total = len(results)
    passed = sum(1 for v in results.values() if v)
    
    if all(results.values()):
        print_success(f"✅ Todas las verificaciones pasaron ({passed}/{total})")
        print_info("\nEl sistema está correctamente configurado.")
        print_info("Puedes iniciar el servidor con:")
        print_info("  uvicorn api.index:app --reload --host 0.0.0.0 --port 8000")
    else:
        print_warning(f"⚠️  Algunas verificaciones fallaron ({passed}/{total})")
        print_info("\nRevisa los errores anteriores y:")
        print_info("  1. Configura las variables de entorno (.env)")
        print_info("  2. Crea los directorios faltantes")
        print_info("  3. Agrega archivos de noticias en DIARIO_OFICIAL_EXPORT")
        print_info("  4. Consulta TROUBLESHOOTING.md para más ayuda")
    
    print(f"\n{bcolors.HEADER}{'='*60}{bcolors.ENDC}\n")
    
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print(f"\n\n{bcolors.WARNING}Validación interrumpida por el usuario{bcolors.ENDC}\n")
        sys.exit(130)
    except Exception as e:
        print(f"\n{bcolors.FAIL}Error inesperado: {e}{bcolors.ENDC}\n")
        import traceback
        traceback.print_exc()
        sys.exit(1)