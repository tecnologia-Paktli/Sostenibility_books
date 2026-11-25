#!/usr/bin/env python3
"""
procesar_csv.py — Limpia los CSV recibidos, los agrega al CSV del tema
correspondiente y conecta cada libro con su portada.

Qué hace:
  1. Lee el CSV recibido detectando codificación (UTF-8 / Latin-1) y separador.
  2. Repara filas rotas por descripciones largas (comas o saltos de línea sin comillas).
  3. Limpia espacios extra, saltos de línea, caracteres invisibles y filas vacías.
  4. Elige el CSV destino por el nombre del archivo:
       historia_nov.csv              -> datos/biblioteca/historia.csv
       astronomia_ludoteca_dic.csv   -> datos/ludoteca/astronomia_ludoteca.csv
     Si ese CSV no existe, lo CREA (en ludoteca si el nombre dice "ludoteca",
     si no en biblioteca).
  5. Conecta las portadas: si la columna de imagen está vacía, incompleta
     (solo la carpeta) o apunta a una ruta vieja, busca la imagen en portadas/
     por nombre de archivo o por el título del libro y escribe el enlace correcto.
  6. Descarta duplicados y agrega solo las filas nuevas.
  7. Con --reconectar revisa TODOS los CSV y arregla las portadas rotas
     (útil cuando subes portadas después de los datos).

Solo usa la biblioteca estándar de Python (3.8+).

Ejemplos:
  python scripts/procesar_csv.py entrada/medicina.csv --repo . --dry-run
  python scripts/procesar_csv.py --repo . --reconectar --dry-run
  python scripts/procesar_csv.py entrada/*.csv --repo . --clave title --reconectar --mover-procesados --commit --push
"""

import argparse
import collections
import csv
import difflib
import io
import os
import re
import shutil
import subprocess
import sys
import unicodedata
import urllib.parse
from datetime import datetime
from pathlib import Path

CARPETAS_IGNORADAS = {".git", ".github", "entrada", "procesados", "rechazados", "node_modules",
                      "scripts", "portadas", "fotosdesitio"}
CARPETA_DATOS = "datos"
CARPETA_PORTADAS = "portadas"
SECCION_DEFAULT = "biblioteca"
SECCION_ESPECIAL = "ludoteca"          # si el nombre del archivo la menciona, va a esta sección
EXT_IMAGEN = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".svg")
UMBRAL_ENCABEZADOS = 0.6
ARTICULOS = ("el", "la", "los", "las", "lo", "un", "una", "unos", "unas", "the")


# ---------------------------------------------------------------- utilidades

def normalizar_nombre(texto):
    """'Descripción ' -> 'descripcion' (para comparar encabezados y nombres)."""
    texto = unicodedata.normalize("NFKD", texto or "")
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "_", texto.lower()).strip("_")


def compacto(texto):
    """'Barroco 1600-1780' -> 'barroco16001780' (para comparar títulos con archivos)."""
    return normalizar_nombre(texto).replace("_", "")


INVISIBLES = re.compile(r"[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f​-‏  ﻿]")


def limpiar_valor(valor):
    if valor is None:
        return ""
    valor = unicodedata.normalize("NFC", str(valor))
    valor = valor.replace(" ", " ")
    valor = INVISIBLES.sub("", valor)
    valor = re.sub(r"\s*[\r\n]+\s*", " ", valor)
    valor = re.sub(r"[ \t]+", " ", valor).strip()
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


def idx_columna(encabezado, *nombres):
    norm = [normalizar_nombre(h) for h in encabezado]
    for n in nombres:
        if n in norm:
            return norm.index(n)
    return None


# ------------------------------------------------------ lectura con reparación

def reparar_filas(filas, n_cols, idx_texto):
    ok, rechazadas, reparadas = [], [], 0
    i = 0
    while i < len(filas):
        fila = list(filas[i])
        i += 1
        if not any(c.strip() for c in fila):
            continue
        fue_reparada = False
        while len(fila) < n_cols and i < len(filas):
            siguiente = filas[i]
            if not any(c.strip() for c in siguiente):
                i += 1
                continue
            if len(siguiente) == n_cols:
                break
            fila = fila[:-1] + [fila[-1] + " " + siguiente[0]] + list(siguiente[1:])
            i += 1
            fue_reparada = True
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
    if preferida:
        i = idx_columna(encabezado, normalizar_nombre(preferida))
        if i is None:
            raise SystemExit(f"La columna de texto '{preferida}' no existe. Columnas: {encabezado}")
        return i
    i = idx_columna(encabezado, "descripcion", "description", "desc", "detalle", "detalles", "comentario",
                    "comentarios", "observaciones", "texto", "resumen", "notas", "contenido")
    if i is not None:
        return i
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
    while encabezado and not encabezado[-1]:
        encabezado.pop()
    n = len(encabezado)
    cuerpo = [f[:n] if len(f) > n and not any(c.strip() for c in f[n:]) else f for f in filas[1:]]
    idx_texto = elegir_columna_texto(encabezado, cuerpo, columna_texto)
    ok, rechazadas, reparadas = reparar_filas(cuerpo, n, idx_texto)
    ok = [[limpiar_valor(c) for c in f] for f in ok]
    ok = [f for f in ok if any(f)]
    return {"encabezado": encabezado, "filas": ok, "rechazadas": rechazadas, "reparadas": reparadas,
            "separador": sep, "codificacion": codificacion, "columna_texto": encabezado[idx_texto]}


# --------------------------------------------------------------- CSV destino

def buscar_csv_destino(repo):
    resultado = []
    for raiz, dirs, archivos in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in CARPETAS_IGNORADAS and not d.startswith(".")]
        resultado += [Path(raiz) / a for a in archivos if a.lower().endswith(".csv")]
    return sorted(resultado)


def info_destino(ruta):
    ruta = Path(ruta)
    if not ruta.exists():
        return {"ruta": ruta, "encabezado": [], "filas": [], "separador": ",", "fin_linea": "\r\n",
                "termina_en_salto": True, "bom": False, "vacio": True}
    crudo = ruta.read_bytes()
    texto, _ = leer_texto(ruta)
    sep = detectar_separador(texto)
    filas = list(csv.reader(io.StringIO(texto), delimiter=sep))
    return {"ruta": ruta, "encabezado": [h.strip() for h in filas[0]] if filas else [],
            "filas": filas[1:], "separador": sep,
            "fin_linea": "\r\n" if "\r\n" in texto[:5000] else "\n",
            "termina_en_salto": texto.endswith(("\n", "\r")),
            "bom": crudo.startswith(b"\xef\xbb\xbf"), "vacio": not texto.strip()}


def parecido(enc_a, enc_b):
    a = {normalizar_nombre(x) for x in enc_a if x}
    b = {normalizar_nombre(x) for x in enc_b if x}
    return len(a & b) / len(a | b) if a and b else 0.0


def ruta_nueva(repo, nombre_archivo, tema=None):
    """Decide dónde crear el CSV de un tema que todavía no existe."""
    partes = normalizar_nombre(tema or Path(nombre_archivo).stem).split("_")
    partes = [p for p in partes if p]
    if not partes:
        raise SystemExit(f"{nombre_archivo}: no puedo deducir el tema del nombre del archivo.")
    es_especial = SECCION_ESPECIAL in partes
    nombre_tema = partes[0] if partes[0] != SECCION_ESPECIAL else (partes[1] if len(partes) > 1 else "")
    if not nombre_tema:
        raise SystemExit(f"{nombre_archivo}: el nombre debe empezar con el tema (p. ej. medicina.csv).")
    seccion = SECCION_ESPECIAL if es_especial else SECCION_DEFAULT
    nombre = f"{nombre_tema}_{SECCION_ESPECIAL}.csv" if es_especial else f"{nombre_tema}.csv"
    base = repo / CARPETA_DATOS / seccion if (repo / CARPETA_DATOS).exists() else repo
    return base / nombre


def elegir_destino(ruta_recibida, encabezado, repo, tema=None):
    """Devuelve (ruta_destino, explicación, es_nuevo)."""
    candidatos = buscar_csv_destino(repo)
    clave = normalizar_nombre(tema) if tema else normalizar_nombre(Path(ruta_recibida).stem)

    # Por nombre: 'historia_nov' -> historia.csv ; el nombre más largo gana
    por_nombre = [c for c in candidatos if clave == normalizar_nombre(c.stem)
                  or (not tema and clave.startswith(normalizar_nombre(c.stem) + "_"))]
    if por_nombre:
        por_nombre.sort(key=lambda c: len(c.stem), reverse=True)
        return por_nombre[0], ("por --tema" if tema else "por nombre de archivo"), False

    # Por encabezados (solo sirve si los CSV tienen columnas distintas entre sí)
    if candidatos and not tema:
        puntajes = sorted(((parecido(encabezado, info_destino(c)["encabezado"]), c) for c in candidatos),
                          key=lambda x: x[0], reverse=True)
        mejor = puntajes[0]
        segundo = puntajes[1][0] if len(puntajes) > 1 else 0
        if mejor[0] >= UMBRAL_ENCABEZADOS and mejor[0] > segundo:
            return mejor[1], f"por encabezados ({mejor[0]:.0%})", False

    # No existe: se crea
    return ruta_nueva(repo, Path(ruta_recibida).name, tema), "tema nuevo: se crea el CSV", True


def encabezado_estandar(repo):
    """El encabezado más usado entre los CSV existentes."""
    cont = collections.Counter(tuple(info_destino(c)["encabezado"]) for c in buscar_csv_destino(repo))
    cont.pop((), None)
    return list(cont.most_common(1)[0][0]) if cont else None


def estilo_mayoritario(repo):
    infos = [info_destino(c) for c in buscar_csv_destino(repo)]
    if not infos:
        return {"bom": False, "fin_linea": "\n"}
    return {"bom": sum(i["bom"] for i in infos) > len(infos) / 2,
            "fin_linea": collections.Counter(i["fin_linea"] for i in infos).most_common(1)[0][0]}


# ------------------------------------------------------------------ portadas

class Portadas:
    """Índice de imágenes en portadas/ y construcción de enlaces raw de GitHub."""

    def __init__(self, repo):
        self.repo = repo
        self.archivos = []
        raiz = repo / CARPETA_PORTADAS
        if raiz.exists():
            for r, _, fs in os.walk(raiz):
                for f in fs:
                    if f.lower().endswith(EXT_IMAGEN):
                        self.archivos.append((Path(r) / f).relative_to(repo).as_posix())
        self.archivos.sort()
        self.por_nfc = {unicodedata.normalize("NFC", p): p for p in self.archivos}
        self.base = self._base_url()
        self.ya_usadas = set()

    # -- base del enlace: https://raw.githubusercontent.com/<dueño>/<repo>/<rama>/
    def _base_url(self):
        conteo = collections.Counter()
        for c in buscar_csv_destino(self.repo):
            for m in re.finditer(r"https://raw\.githubusercontent\.com/[^/\s\"]+/[^/\s\"]+/[^/\s\"]+/",
                                 c.read_text(encoding="utf-8", errors="ignore")):
                conteo[m.group(0)] += 1
        if conteo:
            return conteo.most_common(1)[0][0]
        repo_gh = os.environ.get("GITHUB_REPOSITORY")
        rama = os.environ.get("GITHUB_REF_NAME")
        try:
            if not repo_gh:
                url = subprocess.run(["git", "-C", str(self.repo), "remote", "get-url", "origin"],
                                     capture_output=True, text=True).stdout.strip()
                m = re.search(r"github\.com[:/](.+?/.+?)(\.git)?$", url)
                repo_gh = m.group(1) if m else None
            if not rama:
                rama = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "--abbrev-ref", "HEAD"],
                                      capture_output=True, text=True).stdout.strip() or "main"
        except OSError:
            pass
        return f"https://raw.githubusercontent.com/{repo_gh}/{rama or 'main'}/" if repo_gh else None

    def url(self, ruta):
        # mismo estilo que los enlaces existentes: acentos tal cual, espacios y comas codificados
        cod = "".join(c if (ord(c) > 127 or c.isalnum() or c in "/-_.~()") else urllib.parse.quote(c)
                      for c in ruta)
        return self.base + cod

    def ruta_de(self, valor):
        """Ruta dentro del repo a la que apunta un valor (URL completa, ruta o nombre de archivo)."""
        v = valor.strip()
        if v.startswith("http"):
            u = urllib.parse.urlparse(v)
            partes = urllib.parse.unquote(u.path).split("/")[1:]
            if "raw.githubusercontent.com" in u.netloc and len(partes) > 3:
                return "/".join(partes[3:])
            if "github.com" in u.netloc and "blob" in partes:
                i = partes.index("blob")
                return "/".join(partes[i + 2:])
            return None
        return v.lstrip("./")

    def existe(self, ruta):
        return ruta is not None and unicodedata.normalize("NFC", ruta) in self.por_nfc

    def carpetas_preferidas(self, tema, seccion):
        t = compacto(tema)
        en_seccion = [p for p in self.archivos if seccion and p.startswith(f"{CARPETA_PORTADAS}/{seccion}/")]
        del_tema = [p for p in en_seccion if t and any(
            compacto(parte).startswith(t) or t.startswith(compacto(parte)) and len(compacto(parte)) >= 4
            for parte in p.split("/")[2:-1])]
        return [del_tema, en_seccion, self.archivos]

    @staticmethod
    def variantes_titulo(titulo):
        t = titulo.strip()
        vs = [t]
        m = re.match(r"^(.*?),\s*(" + "|".join(ARTICULOS) + r")\s*$", t, re.I)
        if m:  # 'PRÍNCIPE, EL' -> 'EL PRÍNCIPE'
            vs.append(f"{m.group(2)} {m.group(1)}")
        return [compacto(v) for v in vs if compacto(v)]

    def buscar_por_titulo(self, titulo, grupos):
        variantes = self.variantes_titulo(titulo)
        for grupo in grupos:
            mejores, puntaje_max = [], 0
            for p in grupo:
                nombre = compacto(Path(p).stem)
                sin_num = re.sub(r"\d+$", "", nombre)
                puntaje = 0
                for v in variantes:
                    if nombre == v:
                        puntaje = max(puntaje, 1000)
                    elif len(nombre) >= 5 and v.startswith(nombre):
                        puntaje = max(puntaje, 500 + len(nombre))
                    elif len(sin_num) >= 6 and v.startswith(sin_num):
                        puntaje = max(puntaje, 100 + len(sin_num))
                if puntaje and p in self.ya_usadas:
                    puntaje -= 50
                if puntaje > puntaje_max:
                    mejores, puntaje_max = [p], puntaje
                elif puntaje and puntaje == puntaje_max:
                    mejores.append(p)
            if len(mejores) == 1:
                return mejores[0]
            if len(mejores) > 1:
                return None  # ambiguo: mejor no adivinar
        # Último recurso: nombres casi iguales (una letra de más o de menos), solo si es claro
        for grupo in grupos[:2]:
            parecidos = sorted(((max(difflib.SequenceMatcher(None, v, compacto(Path(p).stem)).ratio()
                                     for v in variantes), p) for p in grupo
                                if len(compacto(Path(p).stem)) >= 8 and p not in self.ya_usadas), reverse=True)
            if parecidos and parecidos[0][0] >= 0.9 and (len(parecidos) == 1 or parecidos[1][0] < parecidos[0][0] - 0.1):
                return parecidos[0][1]
        return None

    def resolver(self, valor, titulo, tema, seccion):
        """Devuelve (nuevo_valor, estado). estado: ok | conectada | sin_portada."""
        ruta = self.ruta_de(valor) if valor.strip() else None
        if self.existe(ruta) and not ruta.endswith("/"):
            if valor != valor.strip():
                return valor.strip(), "conectada"
            return valor, "ok"
        if self.base is None:
            return valor, "sin_portada"
        grupos = self.carpetas_preferidas(tema, seccion)
        # 1) mismo nombre de archivo en otra carpeta (ruta vieja)
        nombre = Path(ruta).name if ruta and not ruta.endswith("/") else ""
        if nombre and "." in nombre:
            for grupo in grupos:
                iguales = [p for p in grupo if unicodedata.normalize("NFC", Path(p).name).lower()
                           == unicodedata.normalize("NFC", nombre).lower()]
                if len(iguales) == 1:
                    self.ya_usadas.add(iguales[0])
                    return self.url(iguales[0]), "conectada"
        # 2) por título
        if titulo:
            p = self.buscar_por_titulo(titulo, grupos)
            if p:
                self.ya_usadas.add(p)
                return self.url(p), "conectada"
        return valor, "sin_portada"


def contexto_destino(ruta, repo):
    """tema y sección a partir de datos/<seccion>/<tema>[_ludoteca].csv"""
    rel = Path(ruta).relative_to(repo).parts
    seccion = rel[1] if len(rel) >= 3 and rel[0] == CARPETA_DATOS else None
    tema = re.sub(r"_" + SECCION_ESPECIAL + r"$", "", normalizar_nombre(Path(ruta).stem))
    return tema, seccion


def escribir_csv(ruta, encabezado, filas, info):
    buf = io.StringIO()
    csv.writer(buf, delimiter=info["separador"], lineterminator=info["fin_linea"]).writerows([encabezado] + filas)
    texto = buf.getvalue()
    if not info.get("termina_en_salto", True):
        texto = texto[: -len(info["fin_linea"])]
    Path(ruta).write_bytes((b"\xef\xbb\xbf" if info["bom"] else b"") + texto.encode("utf-8"))


def reconectar(repo, portadas, dry_run):
    """Revisa todos los CSV y arregla las portadas rotas o vacías."""
    cambiados, pendientes = [], []
    for ruta in buscar_csv_destino(repo):
        info = info_destino(ruta)
        enc = info["encabezado"]
        i_img, i_tit = idx_columna(enc, "image", "imagen", "portada"), idx_columna(enc, "title", "titulo")
        if i_img is None:
            continue
        tema, seccion = contexto_destino(ruta, repo)
        filas = [f for f in info["filas"]]
        n = 0
        for f in filas:
            if len(f) <= i_img or not any(c.strip() for c in f):
                continue
            titulo = f[i_tit] if i_tit is not None and len(f) > i_tit else ""
            nuevo, estado = portadas.resolver(f[i_img], titulo, tema, seccion)
            if estado == "conectada":
                f[i_img] = nuevo
                n += 1
            elif estado == "sin_portada":
                pendientes.append((ruta.relative_to(repo), titulo))
        if n:
            cambiados.append((ruta.relative_to(repo), n))
            if not dry_run:
                escribir_csv(ruta, enc, filas, info)
    print("\n🖼  Revisión de portadas en todos los CSV")
    for r, n in cambiados:
        print(f"   {r}: {n} portada(s) conectada(s)")
    if not cambiados:
        print("   no había portadas que conectar")
    if pendientes:
        print(f"   ⚠ {len(pendientes)} libro(s) sin portada todavía (sube la imagen a {CARPETA_PORTADAS}/):")
        for r, t in pendientes[:30]:
            print(f"     - {r}: {t}")
    return bool(cambiados)


# ------------------------------------------------------------------ proceso

def procesar(ruta, args, repo, portadas):
    rec = leer_csv_recibido(ruta, args.columna_texto)
    destino_ruta, metodo, nuevo = elegir_destino(ruta, rec["encabezado"], repo, args.tema)
    if nuevo:
        estandar = encabezado_estandar(repo)
        enc_dst = estandar if estandar and parecido(estandar, rec["encabezado"]) >= UMBRAL_ENCABEZADOS \
            else rec["encabezado"]
        dst = info_destino(destino_ruta)
        dst.update(estilo_mayoritario(repo))
    else:
        dst = info_destino(destino_ruta)
        enc_dst = dst["encabezado"] or rec["encabezado"]

    mapa = {normalizar_nombre(h): i for i, h in enumerate(rec["encabezado"])}
    faltantes = [h for h in enc_dst if normalizar_nombre(h) not in mapa]
    sobrantes = [h for h in rec["encabezado"] if normalizar_nombre(h) not in {normalizar_nombre(x) for x in enc_dst}]
    if faltantes and len(faltantes) == len(enc_dst):
        raise SystemExit(f"ninguna columna coincide con {destino_ruta.name}.")
    filas = [[f[mapa[normalizar_nombre(h)]] if normalizar_nombre(h) in mapa else "" for h in enc_dst]
             for f in rec["filas"]]

    # Portadas
    tema, seccion = contexto_destino(destino_ruta, repo)
    i_img, i_tit = idx_columna(enc_dst, "image", "imagen", "portada"), idx_columna(enc_dst, "title", "titulo")
    conectadas, sin_portada = 0, []
    if i_img is not None:
        for f in filas:
            titulo = f[i_tit] if i_tit is not None else ""
            f[i_img], estado = portadas.resolver(f[i_img], titulo, tema, seccion)
            conectadas += estado == "conectada"
            if estado == "sin_portada":
                sin_portada.append(titulo)

    # Duplicados
    if args.clave:
        claves = [normalizar_nombre(c) for c in args.clave.split(",")]
        idx = [i for i, h in enumerate(enc_dst) if normalizar_nombre(h) in claves] or list(range(len(enc_dst)))
    else:
        idx = list(range(len(enc_dst)))
    firma = lambda f: tuple(limpiar_valor(f[i]).casefold() if i < len(f) else "" for i in idx)
    vistas = {firma(f) for f in dst["filas"]}
    nuevas, duplicadas = [], 0
    for f in filas:
        k = firma(f)
        if k in vistas:
            duplicadas += 1
            continue
        vistas.add(k)
        nuevas.append(f)

    rel = destino_ruta.relative_to(repo)
    print(f"\n📄 {ruta}")
    print(f"   destino: {rel}  ({metodo}){'  🆕' if nuevo else ''}")
    print(f"   leído como {rec['codificacion']}, separador {rec['separador']!r}, texto largo en '{rec['columna_texto']}'")
    print(f"   filas válidas: {len(rec['filas'])} | reparadas: {rec['reparadas']} | duplicadas omitidas: {duplicadas}"
          f" | nuevas: {len(nuevas)} | rechazadas: {len(rec['rechazadas'])} | portadas conectadas: {conectadas}")
    if faltantes:
        print(f"   ⚠ columnas que no venían (se dejan vacías): {', '.join(faltantes)}")
    if sobrantes:
        print(f"   ⚠ columnas que no existen en el destino (se ignoran): {', '.join(sobrantes)}")
    if sin_portada:
        carpeta = f"{CARPETA_PORTADAS}/{seccion or SECCION_DEFAULT}/{tema}/"
        print(f"   ⚠ {len(sin_portada)} libro(s) sin portada; sube sus imágenes a {carpeta} "
              f"(el robot las conectará solo):")
        for t in sin_portada[:15]:
            print(f"     - {t}")

    if rec["rechazadas"]:
        carpeta = repo / "rechazados"
        salida = carpeta / f"{Path(ruta).stem}_rechazadas_{datetime.now():%Y%m%d_%H%M%S}.csv"
        if not args.dry_run:
            carpeta.mkdir(exist_ok=True)
            with open(salida, "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(rec["encabezado"])
                w.writerows(rec["rechazadas"])
        print(f"   ⚠ filas que no pude reparar → {salida.relative_to(repo)} (revísalas a mano)")

    if args.dry_run:
        for f in nuevas[:2]:
            print("   vista previa:", {k: (v[:70] + "…" if len(v) > 70 else v) for k, v in zip(enc_dst, f)})
        return

    if nuevas or nuevo:
        destino_ruta.parent.mkdir(parents=True, exist_ok=True)
        if dst["vacio"]:
            escribir_csv(destino_ruta, enc_dst, nuevas, dst)
        else:
            with open(destino_ruta, "a", newline="", encoding="utf-8") as fh:
                if not dst["termina_en_salto"]:
                    fh.write(dst["fin_linea"])
                csv.writer(fh, delimiter=dst["separador"], lineterminator=dst["fin_linea"]).writerows(nuevas)

    if args.mover_procesados:
        carpeta = Path(ruta).parent / "procesados"
        carpeta.mkdir(exist_ok=True)
        shutil.move(str(ruta), carpeta / f"{Path(ruta).stem}_{datetime.now():%Y%m%d_%H%M%S}{Path(ruta).suffix}")


def git(repo, *cmd):
    return subprocess.run(["git", "-C", str(repo), *cmd], check=True, capture_output=True, text=True)


def main():
    p = argparse.ArgumentParser(description="Limpia CSV recibidos, los agrega al CSV del tema y conecta portadas.")
    p.add_argument("archivos", nargs="*", help="CSV recibidos (opcional si usas --reconectar)")
    p.add_argument("--repo", default=".", help="Carpeta del repositorio (default: .)")
    p.add_argument("--tema", help="Nombre del CSV destino sin .csv (si no, se toma del nombre del archivo)")
    p.add_argument("--columna-texto", help="Columna con texto largo que puede traer comas")
    p.add_argument("--clave", help="Columna(s) que identifican un libro único, p. ej. title")
    p.add_argument("--reconectar", action="store_true", help="Arregla portadas rotas o vacías en todos los CSV")
    p.add_argument("--dry-run", action="store_true", help="Solo muestra lo que haría, sin escribir nada")
    p.add_argument("--mover-procesados", action="store_true", help="Mueve cada archivo a <carpeta>/procesados/")
    p.add_argument("--commit", action="store_true", help="Hace git commit de los cambios")
    p.add_argument("--push", action="store_true", help="Hace git push después del commit")
    args = p.parse_args()
    if not args.archivos and not args.reconectar:
        p.error("indica al menos un CSV o usa --reconectar")

    repo = Path(args.repo).resolve()
    if args.commit and not args.dry_run:
        try:
            git(repo, "rev-parse", "--abbrev-ref", "@{u}")
            git(repo, "pull", "--rebase", "--autostash")
        except subprocess.CalledProcessError:
            pass

    portadas = Portadas(repo)
    errores = 0
    for archivo in args.archivos:
        try:
            procesar(archivo, args, repo, portadas)
        except SystemExit as e:
            errores += 1
            print(f"\n❌ {archivo}: {e}")

    if args.reconectar:
        reconectar(repo, portadas, args.dry_run)

    if args.commit and not args.dry_run:
        cambios = git(repo, "status", "--porcelain").stdout.strip()
        if cambios:
            git(repo, "add", "-A")
            tocados = sorted({l[3:].strip('"').split(" -> ")[-1] for l in cambios.splitlines()
                              if l[3:].strip('"').startswith(CARPETA_DATOS)})
            msg = "Robot: actualizar " + (", ".join(tocados[:6]) + ("…" if len(tocados) > 6 else "")
                                          if tocados else "archivos procesados")
            git(repo, "commit", "-m", msg)
            print(f"\n✅ commit: {msg}")
            if args.push:
                git(repo, "push")
                print("✅ push hecho")
        else:
            print("\nNo hubo cambios que guardar.")
    sys.exit(1 if errores else 0)


if __name__ == "__main__":
    main()
