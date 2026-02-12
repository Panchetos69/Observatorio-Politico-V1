#!/usr/bin/env python3
"""
Script de diagnóstico para verificar la carga de noticias del Diario Oficial
"""
import os
import json
import sys

# Simular la estructura del proyecto
BASE_DIR = "/mnt/user-data/uploads"  # Ajusta según tu estructura real

def test_news_loading():
    print("=" * 60)
    print("DIAGNÓSTICO: Carga de Noticias - Diario Oficial")
    print("=" * 60)
    
    # 1. Verificar estructura de directorios
    print("\n1. Verificando estructura de directorios:")
    print(f"   BASE_DIR: {BASE_DIR}")
    
    # Buscar DIARIO_OFICIAL_EXPORT
    diario_dir = None
    possible_paths = [
        os.path.join(BASE_DIR, "DIARIO_OFICIAL_EXPORT"),
        os.path.join(BASE_DIR, "..", "DIARIO_OFICIAL_EXPORT"),
        "DIARIO_OFICIAL_EXPORT"
    ]
    
    for path in possible_paths:
        abs_path = os.path.abspath(path)
        print(f"   Probando: {abs_path}")
        if os.path.isdir(abs_path):
            diario_dir = abs_path
            print(f"   ✓ Encontrado en: {diario_dir}")
            break
    else:
        print("   ✗ No se encontró DIARIO_OFICIAL_EXPORT")
        return
    
    # 2. Listar archivos en DIARIO_OFICIAL_EXPORT
    print(f"\n2. Archivos en {diario_dir}:")
    try:
        files = os.listdir(diario_dir)
        for f in sorted(files):
            fpath = os.path.join(diario_dir, f)
            size = os.path.getsize(fpath)
            print(f"   - {f} ({size:,} bytes)")
    except Exception as e:
        print(f"   ✗ Error listando archivos: {e}")
        return
    
    # 3. Buscar archivos JSON (no logs)
    print("\n3. Buscando archivos JSON (excluyendo logs):")
    json_files = [
        f for f in files 
        if f.endswith('.json') and 'log' not in f.lower()
    ]
    
    if json_files:
        print(f"   Encontrados {len(json_files)} archivo(s) JSON:")
        for jf in json_files:
            print(f"   - {jf}")
    else:
        print("   ✗ No se encontraron archivos JSON (sin 'log' en el nombre)")
    
    # 4. Buscar archivos CSV
    print("\n4. Buscando archivos CSV:")
    csv_files = [f for f in files if f.endswith('.csv')]
    if csv_files:
        print(f"   Encontrados {len(csv_files)} archivo(s) CSV:")
        for cf in csv_files:
            print(f"   - {cf}")
    else:
        print("   ✗ No se encontraron archivos CSV")
    
    # 5. Intentar leer el archivo más reciente
    candidates = json_files if json_files else csv_files
    if not candidates:
        print("\n✗ No hay archivos para procesar")
        return
    
    # Ordenar por fecha de modificación (más reciente primero)
    candidates_full = [os.path.join(diario_dir, f) for f in candidates]
    latest_file = max(candidates_full, key=lambda p: os.path.getmtime(p))
    
    print(f"\n5. Procesando archivo más reciente:")
    print(f"   Archivo: {os.path.basename(latest_file)}")
    
    # 6. Leer y parsear el archivo
    if latest_file.endswith('.json'):
        print("\n6. Leyendo archivo JSON:")
        try:
            with open(latest_file, 'r', encoding='utf-8-sig', errors='ignore') as f:
                content = f.read().strip()
            
            items = []
            
            # Detectar si es JSON array o JSONL
            if content.startswith('['):
                print("   Formato: JSON Array")
                data = json.loads(content)
                if isinstance(data, list):
                    items = [x for x in data if isinstance(x, dict)]
            else:
                print("   Formato: JSONL (una línea por objeto)")
                for line_no, line in enumerate(content.splitlines(), 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        if isinstance(obj, dict):
                            items.append(obj)
                    except Exception as e:
                        print(f"   ⚠ Error en línea {line_no}: {e}")
            
            print(f"   ✓ Se cargaron {len(items)} items")
            
            # 7. Mostrar muestra de datos
            if items:
                print("\n7. Muestra de datos (primeros 3 items):")
                for i, item in enumerate(items[:3], 1):
                    print(f"\n   Item #{i}:")
                    for key in ['titulo', 'title', 'fecha', 'date', 'pdf_url', 'url', 'tab', 'cve']:
                        if key in item:
                            value = str(item[key])[:100]
                            print(f"      {key}: {value}")
                
                print(f"\n   Claves disponibles en items:")
                all_keys = set()
                for item in items:
                    all_keys.update(item.keys())
                print(f"      {', '.join(sorted(all_keys))}")
            else:
                print("\n   ✗ No se encontraron items en el archivo")
                
        except Exception as e:
            print(f"   ✗ Error leyendo JSON: {e}")
            import traceback
            traceback.print_exc()
    
    elif latest_file.endswith('.csv'):
        print("\n6. Leyendo archivo CSV:")
        try:
            import csv
            with open(latest_file, 'r', encoding='utf-8-sig', errors='ignore', newline='') as f:
                reader = csv.DictReader(f)
                items = []
                for row in reader:
                    clean_row = {}
                    for k, v in (row or {}).items():
                        if k is None:
                            continue
                        kk = str(k).replace('\ufeff', '').strip()
                        vv = v.strip() if isinstance(v, str) else v
                        clean_row[kk] = vv
                    items.append(clean_row)
            
            print(f"   ✓ Se cargaron {len(items)} items")
            
            # Mostrar muestra
            if items:
                print("\n7. Muestra de datos (primeros 3 items):")
                for i, item in enumerate(items[:3], 1):
                    print(f"\n   Item #{i}:")
                    for key, value in list(item.items())[:8]:
                        value_str = str(value)[:100]
                        print(f"      {key}: {value_str}")
                
                print(f"\n   Columnas disponibles:")
                print(f"      {', '.join(items[0].keys())}")
            else:
                print("\n   ✗ No se encontraron items en el archivo")
                
        except Exception as e:
            print(f"   ✗ Error leyendo CSV: {e}")
            import traceback
            traceback.print_exc()
    
    print("\n" + "=" * 60)
    print("Diagnóstico completado")
    print("=" * 60)

if __name__ == "__main__":
    test_news_loading()