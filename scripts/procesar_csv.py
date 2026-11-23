#!/usr/bin/env python3
"""
procesar_csv.py — Limpia un CSV recibido y agrega sus filas al CSV temático
correspondiente dentro de un repositorio.

Qué hace:
  1. Lee el CSV recibido detectando codificación (UTF-8 / Latin-1) y separador.
  2. Repara filas rotas por descripciones largas:
       - comas sin comillas dentro del texto (sobran columnas)
       - saltos de línea sin comillas dentro del texto (faltan columnas)
  3. Limpia: espacios extra, saltos de línea, caracteres invisibles, filas vacías.
  4. Detecta el CSV destino (por --tema, por nombre de archivo o por encabezados).
  5. Ordena las columnas como en el destino, descarta duplicados y agrega
     solo las filas nuevas, citando bien los textos con comas.
  6. Opcional: mueve el archivo procesado y hace git commit/push.

Solo usa la biblioteca estándar de Python (3.8+).

Ejemplos:
  python scripts/procesar_csv.py nuevos.csv --repo .
  python scripts/procesar_csv.py nuevos.csv --repo . --tema eventos --dry-run
  python scripts/procesar_csv.py entrada/*.csv --repo . --mover-procesados --commit --push
"""

import argparse
import csv
import io
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

CARPETAS_IGNORADAS = {".git", ".github", "entrada", "procesados", "rechazados", "node_modules", "scripts"}
UMBRAL_ENCABEZADOS = 0.6  # parecido mínimo de encabezados para auto-detectar el tema


# ---------------------------------------------------------------- utilidades

def normalizar_nombre(texto):
    """'Descripción ' -> 'descripcion' (para comparar encabezados y nombres)."""
    texto = unicodedata.normalize("NFKD", texto or "")
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    texto = re.sub(r"[^a-z0-9]+", "_", texto.lower()).strip("_")
    return texto


INVISIBLES = re.compile(r"[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f​-‏  ﻿]")


def limpiar_valor(valor):
    """Quita invisibles, une saltos de línea y colapsa espacios."""
    if valor is None:
        return ""
    valor = unicodedata.normalize("NFC", str(valor))
    valor = valor.replace(" ", " ")
    valor = INVISIBLES.sub("", valor)
    valor = re.sub(r"\s*[\r\n]+\s*", " ", valor)
    valor = re.sub(r"[ \t]+", " ", valor)
    valor = valor.strip()
    # comillas sobrantes que envuelven todo el valor:  ""texto""  ->  texto
    if len(valor) >= 2 and valor[0] == valor[-1] == '"':
        valor = valor[1:-1].strip()
    return valor


def leer_texto(ruta):
    datos = Path(ruta).read_bytes()
    for cod in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return datos.decode(cod), cod
        except UnicodeDecodeError:
            continue
    return datos.decode("utf-8", errors="replace"), "utf-8 (con reemplazos)"


def detectar_separador(texto):
    primera = texto.splitlines()[0] if texto else ""
    candidatos = {sep: primera.count(sep) for sep in [",", ";", "\t", "|"]}
    mejor = max(candidatos, key=candidatos.get)
    return mejor if candidatos[mejor] > 0 else ","


# ------------------------------------------------------ lectura con reparación

def reparar_filas(filas, n_cols, idx_texto):
    """Devuelve (filas_ok, filas_rechazadas, n_reparadas)."""
    ok, rechazadas, reparadas = [], [], 0
    i = 0
    while i < len(filas):
        fila = list(filas[i])
        i += 1
        if not any(c.strip() for c in fila):
            continue  # fila vacía
        fue_reparada = False

        # Faltan columnas: probablemente un salto de línea sin comillas en el texto.
        while len(fila) < n_cols and i < len(filas):
            siguiente = filas[i]
            if not any(c.strip() for c in siguiente):
                i += 1
                continue
            # Si la siguiente fila por sí sola está completa, no la pegamos.
            if len(siguiente) == n_cols:
                break
            fila = fila[:-1] + [fila[-1] + " " + siguiente[0]] + list(siguiente[1:])
            i += 1
            fue_reparada = True

        # Sobran columnas: comas sin comillas dentro de la columna de texto largo.
        if len(fila) > n_cols:
            extra = len(fila) - n_cols
            unido = ",".join(fila[idx_texto: idx_texto + extra + 1])
            fila = fila[:idx_texto] + [unido] + fila[idx_texto + extra + 1:]
            fue_reparada = True

        if len(fila) != n_cols:
            rechazadas.append(fila)
            continue
        ok.append(fila)
        reparadas += fue_reparada
    return ok, rechazadas, reparadas


def elegir_columna_texto(encabezado, filas, preferida=None):
    nombres = [normalizar_nombre(h) for h in encabezado]
    if preferida:
        p = normalizar_nombre(preferida)
        if p in nombres:
            return nombres.index(p)
        raise SystemExit(f"La columna de texto '{preferida}' no existe. Columnas: {encabezado}")
    # Nombres típicos
    for clave in ("descripcion", "description", "desc", "detalle", "detalles", "comentario",
                  "comentarios", "observaciones", "texto", "resumen", "notas", "contenido"):
        if clave in nombres:
            return nombres.index(clave)
    # Si no, la columna con textos más largos entre las filas bien formadas
    bien = [f for f in filas if len(f) == len(encabezado)]
    if not bien:
        return len(encabezado) - 1
    promedios = [sum(len(f[j]) for f in bien) / len(bien) for j in range(len(encabezado))]
    return max(range(len(encabezado)), key=lambda j: promedios[j])


def leer_csv_recibido(ruta, columna_texto=None):
    texto, codificacion = leer_texto(ruta)
    sep = detectar_separador(texto)
    filas = list(csv.reader(io.StringIO(texto), delimiter=sep))
    while filas and not any(c.strip() for c in filas[0]):
        filas.pop(0)
    if not filas:
        raise SystemExit(f"{ruta}: el archivo está vacío.")
    encabezado = [limpiar_valor(h) for h in filas[0]]
    while encabezado and not encabezado[-1]:  # columnas vacías al final ("a,b,c,,")
        encabezado.pop()
    cuerpo = filas[1:]
    n = len(encabezado)
    # quitar celdas vacías finales que sobran (exportaciones de Excel)
    cuerpo = [f[:n] if len(f) > n and not any(c.strip() for c in f[n:]) else f for f in cuerpo]
    idx_texto = elegir_columna_texto(encabezado, cuerpo, columna_texto)
    ok, rechazadas, reparadas = reparar_filas(cuerpo, n, idx_texto)
    ok = [[limpiar_valor(c) for c in f] for f in ok]
    ok = [f for f in ok if any(f)]
    return {
        "encabezado": encabezado, "filas": ok, "rechazadas": rechazadas,
        "reparadas": reparadas, "separador": sep, "codificacion": codificacion,
        "columna_texto": encabezado[idx_texto],
    }


# --------------------------------------------------------------- CSV destino

def buscar_csv_destino(repo):
    resultado = []
    for raiz, dirs, archivos in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in CARPETAS_IGNORADAS and not d.startswith(".")]
        for a in archivos:
            if a.lower().endswith(".csv"):
                resultado.append(Path(raiz) / a)
    return sorted(resultado)


def info_destino(ruta):
    texto, cod = leer_texto(ruta)
    sep = detectar_separador(texto)
    filas = list(csv.reader(io.StringIO(texto), delimiter=sep))
    encabezado = [h.strip().lstrip("﻿") for h in filas[0]] if filas else []
    fin_linea = "\r\n" if "\r\n" in texto[:5000] else "\n"
    return {
        "ruta": Path(ruta), "encabezado": encabezado, "filas": filas[1:],
        "separador": sep, "fin_linea": fin_linea, "termina_en_salto": texto.endswith(("\n", "\r")),
        "codificacion": "utf-8-sig" if cod == "utf-8-sig" and Path(ruta).read_bytes()[:3] == b"\xef\xbb\xbf" else ("utf-8" if cod.startswith("utf-8") else cod),
        "vacio": not texto.strip(),
    }


def parecido(enc_a, enc_b):
    a = {normalizar_nombre(x) for x in enc_a if x}
    b = {normalizar_nombre(x) for x in enc_b if x}
    return len(a & b) / len(a | b) if a and b else 0.0


def elegir_destino(ruta_recibida, encabezado, repo, tema=None):
    candidatos = buscar_csv_destino(repo)
    if not candidatos:
        raise SystemExit(f"No encontré CSV destino dentro de {repo}.")

    if tema:
        t = normalizar_nombre(tema)
        exactos = [c for c in candidatos if normalizar_nombre(c.stem) == t]
        if len(exactos) == 1:
            return exactos[0], "por --tema"
        raise SystemExit(f"--tema '{tema}' no coincide con un único CSV. Disponibles: "
                         + ", ".join(str(c.relative_to(repo)) for c in candidatos))

    # Por nombre de archivo: 'eventos_noviembre.csv' -> eventos.csv
    nombre = normalizar_nombre(Path(ruta_recibida).stem)
    por_nombre = [c for c in candidatos if nombre == normalizar_nombre(c.stem)
                  or nombre.startswith(normalizar_nombre(c.stem) + "_")]
    if len(por_nombre) > 1:  # quedarse con el nombre más largo/específico
        por_nombre.sort(key=lambda c: len(c.stem), reverse=True)
    if por_nombre:
        return por_nombre[0], "por nombre de archivo"

    # Por encabezados
    puntajes = sorted(((parecido(encabezado, info_destino(c)["encabezado"]), c) for c in candidatos),
                      key=lambda x: x[0], reverse=True)
    mejor, segundo = puntajes[0], (puntajes[1] if len(puntajes) > 1 else (0, None))
    if mejor[0] >= UMBRAL_ENCABEZADOS and mejor[0] > segundo[0]:
        return mejor[1], f"por encabezados ({mejor[0]:.0%} de coincidencia)"
    raise SystemExit(
        "No pude decidir a qué CSV va este archivo. Usa --tema o nombra el archivo como el destino "
        "(p. ej. 'eventos_nov.csv'). Mejores coincidencias: "
        + ", ".join(f"{c.relative_to(repo)} ({p:.0%})" for p, c in puntajes[:3]))


# ------------------------------------------------------------------ proceso

def procesar(ruta, args):
    repo = Path(args.repo).resolve()
    rec = leer_csv_recibido(ruta, args.columna_texto)
    destino_ruta, metodo = elegir_destino(ruta, rec["encabezado"], repo, args.tema)
    dst = info_destino(destino_ruta)

    enc_dst = dst["encabezado"] or rec["encabezado"]
    mapa = {normalizar_nombre(h): i for i, h in enumerate(rec["encabezado"])}
    faltantes = [h for h in enc_dst if normalizar_nombre(h) not in mapa]
    sobrantes = [h for h in rec["encabezado"] if normalizar_nombre(h) not in {normalizar_nombre(x) for x in enc_dst}]

    if faltantes and len(faltantes) == len(enc_dst):
        raise SystemExit(f"{ruta}: ninguna columna coincide con {destino_ruta.name}.")

    filas_alineadas = [[f[mapa[normalizar_nombre(h)]] if normalizar_nombre(h) in mapa else "" for h in enc_dst]
                       for f in rec["filas"]]

    # Duplicados (contra el destino y dentro del mismo archivo)
    if args.clave:
        claves = [normalizar_nombre(c) for c in args.clave.split(",")]
        idx = [i for i, h in enumerate(enc_dst) if normalizar_nombre(h) in claves]
        if not idx:
            raise SystemExit(f"La(s) columna(s) clave '{args.clave}' no existen en {destino_ruta.name}.")
    else:
        idx = list(range(len(enc_dst)))
    firma = lambda f: tuple(limpiar_valor(f[i]).casefold() if i < len(f) else "" for i in idx)
    vistas = {firma(f) for f in dst["filas"]}
    nuevas, duplicadas = [], 0
    for f in filas_alineadas:
        k = firma(f)
        if k in vistas:
            duplicadas += 1
            continue
        vistas.add(k)
        nuevas.append(f)

    rel = destino_ruta.relative_to(repo)
    print(f"\n📄 {ruta}")
    print(f"   destino: {rel}  ({metodo})")
    print(f"   leído como {rec['codificacion']}, separador {rec['separador']!r}, texto largo en '{rec['columna_texto']}'")
    print(f"   filas válidas: {len(rec['filas'])} | reparadas: {rec['reparadas']} | "
          f"duplicadas omitidas: {duplicadas} | nuevas: {len(nuevas)} | rechazadas: {len(rec['rechazadas'])}")
    if faltantes:
        print(f"   ⚠ columnas que no venían (se dejan vacías): {', '.join(faltantes)}")
    if sobrantes:
        print(f"   ⚠ columnas que no existen en el destino (se ignoran): {', '.join(sobrantes)}")

    if rec["rechazadas"]:
        carpeta = repo / "rechazados"
        carpeta.mkdir(exist_ok=True)
        salida = carpeta / f"{Path(ruta).stem}_rechazadas_{datetime.now():%Y%m%d_%H%M%S}.csv"
        if not args.dry_run:
            with open(salida, "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(rec["encabezado"])
                w.writerows(rec["rechazadas"])
        print(f"   ⚠ filas que no pude reparar → {salida.relative_to(repo)} (revísalas a mano)")

    if args.dry_run:
        for f in nuevas[:3]:
            print("   vista previa:", dict(zip(enc_dst, f)))
        return rel if nuevas else None

    if nuevas:
        with open(destino_ruta, "a", newline="", encoding="utf-8" if dst["vacio"] else dst["codificacion"].replace("utf-8-sig", "utf-8")) as fh:
            if not dst["vacio"] and not dst["termina_en_salto"]:
                fh.write(dst["fin_linea"])
            w = csv.writer(fh, delimiter=dst["separador"], lineterminator=dst["fin_linea"],
                           quoting=csv.QUOTE_MINIMAL)
            if dst["vacio"]:
                w.writerow(enc_dst)
            w.writerows(nuevas)

    if args.mover_procesados:
        carpeta = Path(ruta).parent / "procesados"
        carpeta.mkdir(exist_ok=True)
        destino_mov = carpeta / f"{Path(ruta).stem}_{datetime.now():%Y%m%d_%H%M%S}{Path(ruta).suffix}"
        shutil.move(str(ruta), destino_mov)
    return rel if nuevas else None


def git(repo, *cmd):
    return subprocess.run(["git", "-C", str(repo), *cmd], check=True, capture_output=True, text=True)


def main():
    p = argparse.ArgumentParser(description="Limpia CSV recibidos y los agrega al CSV temático del repo.")
    p.add_argument("archivos", nargs="+", help="CSV recibidos")
    p.add_argument("--repo", default=".", help="Carpeta del repositorio (default: .)")
    p.add_argument("--tema", help="Nombre del CSV destino sin .csv (si no, se detecta solo)")
    p.add_argument("--columna-texto", help="Columna con texto largo que puede traer comas (si no, se detecta)")
    p.add_argument("--clave", help="Columna(s) que identifican una fila única, separadas por coma (p. ej. id)")
    p.add_argument("--dry-run", action="store_true", help="Solo muestra lo que haría, sin escribir nada")
    p.add_argument("--mover-procesados", action="store_true", help="Mueve cada archivo a <carpeta>/procesados/")
    p.add_argument("--commit", action="store_true", help="Hace git commit de los cambios")
    p.add_argument("--push", action="store_true", help="Hace git push después del commit")
    args = p.parse_args()

    repo = Path(args.repo).resolve()
    if args.commit and not args.dry_run:
        try:
            git(repo, "rev-parse", "--abbrev-ref", "@{u}")  # ¿hay rama remota?
            git(repo, "pull", "--rebase", "--autostash")
        except subprocess.CalledProcessError:
            pass  # sin remoto o sin conexión: se trabaja con la copia local

    actualizados, errores = set(), 0
    for archivo in args.archivos:
        try:
            r = procesar(archivo, args)
            if r:
                actualizados.add(str(r))
        except SystemExit as e:
            errores += 1
            print(f"\n❌ {archivo}: {e}")

    if args.commit and not args.dry_run and actualizados:
        git(repo, "add", "-A")
        msg = "Agregar datos a " + ", ".join(sorted(actualizados))
        git(repo, "commit", "-m", msg)
        print(f"\n✅ commit: {msg}")
        if args.push:
            git(repo, "push")
            print("✅ push hecho")
    elif not actualizados:
        print("\nNo hubo filas nuevas que agregar.")
    sys.exit(1 if errores else 0)


if __name__ == "__main__":
    main()
