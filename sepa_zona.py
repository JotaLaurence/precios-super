#!/usr/bin/env python3
"""
sepa_zona.py - Prepara los precios SEPA (Precios Claros) para una zona chica.

Baja el archivo diario de SEPA, se queda con las sucursales de la zona
(Martinez, Villa Adelina, Carapachay) y genera JSON livianos en ./salida:

  sucursales.json  sucursales de la zona (cadena, direccion, distancia)
  catalogo.json    {ean: [descripcion, marca, presentacion]}
  precios.json     {ean: {cadena: [precio_lista, precio_promo, leyenda]}}
  indice.json      fecha de los datos, cadenas y cantidades

Uso:
  python sepa_zona.py --listar          # solo muestra las sucursales encontradas
  python sepa_zona.py                   # genera los archivos
  python sepa_zona.py --zip sepa.zip    # usa un zip ya descargado

Datos: Precios Claros - Base SEPA, Secretaria de Comercio Interior
(datos.produccion.gob.ar), licencia CC BY 4.0.
"""
import argparse
import csv
import datetime
import io
import json
import math
import os
import re
import sys
import tempfile
import time
import unicodedata
import urllib.request
import zipfile

CKAN_URL = "https://datos.produccion.gob.ar/api/3/action/package_show?id=sepa-precios"

# Centros aproximados de la zona (lat, lon). Ajustalos si hace falta.
CENTROS = {
    "Martinez": (-34.4906, -58.4980),
    "Villa Adelina": (-34.5148, -58.5469),
    "Carapachay": (-34.5303, -58.5322),
}
LOCALIDADES = {"MARTINEZ", "VILLA ADELINA", "CARAPACHAY"}
DESCUENTO_MAX = 0.70  # promos con mas de 70% de descuento se descartan (suelen ser errores o cuotas)


# ---------- utilidades ----------

def norm(texto):
    t = unicodedata.normalize("NFKD", texto or "")
    return "".join(c for c in t if not unicodedata.combining(c)).upper().strip()


def km(lat1, lon1, lat2, lon2):
    p = math.pi / 180
    a = (math.sin((lat2 - lat1) * p / 2) ** 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2)
    return 12742 * math.asin(math.sqrt(a))


def numero(texto):
    try:
        return float(str(texto).strip().replace(",", "."))
    except ValueError:
        return None


def ix(cols, parte):
    """Indice de la primera columna cuyo nombre contiene `parte`."""
    for i, c in enumerate(cols):
        if parte in c:
            return i
    return None


def v(fila, i):
    return fila[i].strip() if i is not None and i < len(fila) else ""


# ---------- lectura del zip ----------

def filas(zf, nombre):
    """Primero entrega la lista de columnas (en minuscula) y despues cada fila."""
    with zf.open(nombre) as crudo:
        texto = io.TextIOWrapper(crudo, encoding="utf-8-sig", errors="replace", newline="")
        cabecera = texto.readline()
        sep = max("|;,", key=cabecera.count)
        yield [c.strip().strip('"').lower() for c in cabecera.rstrip("\r\n").split(sep)]
        for fila in csv.reader(texto, delimiter=sep):
            yield fila


def buscar(zf, sufijo):
    for n in zf.namelist():
        if n.lower().endswith(sufijo):
            return n
    return None


def comercios(ruta_zip):
    """El archivo SEPA es un zip de zips (uno por comercio)."""
    with zipfile.ZipFile(ruta_zip) as externo:
        internos = [n for n in externo.namelist() if n.lower().endswith(".zip")]
        if not internos:
            yield externo
            return
        for n in internos:
            with externo.open(n) as f:
                datos = f.read()
            try:
                interno = zipfile.ZipFile(io.BytesIO(datos))
            except zipfile.BadZipFile:
                print(f"  aviso: {n} no es un zip valido", file=sys.stderr)
                continue
            yield interno
            interno.close()


def fecha_del_zip(ruta_zip):
    with zipfile.ZipFile(ruta_zip) as z:
        for n in z.namelist():
            m = re.search(r"\d{4}-\d{2}-\d{2}", n)
            if m:
                return m.group(0)
    return None


# ---------- descarga ----------

def pedir(url):
    return urllib.request.Request(url, headers={"User-Agent": "sepa-zona/1.0"})


def abrir_con_reintentos(url, timeout=90, intentos=5):
    for n in range(1, intentos + 1):
        try:
            return urllib.request.urlopen(pedir(url), timeout=timeout)
        except Exception as e:  # noqa: BLE001
            print(f"  intento {n}/{intentos} fallo: {e}", file=sys.stderr)
            if n == intentos:
                raise
            time.sleep(15 * n)


def elegir_recurso(fecha=None):
    with abrir_con_reintentos(CKAN_URL) as r:
        recursos = json.load(r)["result"]["resources"]
    candidatos = []
    for rec in recursos:
        nombre = rec.get("name", "")
        m = re.search(r"(\d{4}-\d{2}-\d{2})", nombre)
        if m and "MINORISTA" in norm(nombre):
            candidatos.append((m.group(1), rec["url"]))
    if fecha:
        candidatos = [c for c in candidatos if c[0] == fecha]
    if not candidatos:
        sys.exit("No encontre un archivo SEPA minorista" + (f" para {fecha}" if fecha else ""))
    return max(candidatos)


def bajar(url, destino, intentos=3):
    for n in range(1, intentos + 1):
        try:
            with urllib.request.urlopen(pedir(url), timeout=120) as r, open(destino, "wb") as f:
                while True:
                    bloque = r.read(1 << 20)
                    if not bloque:
                        break
                    f.write(bloque)
            return
        except Exception as e:  # noqa: BLE001
            print(f"  intento {n}/{intentos} fallo: {e}", file=sys.stderr)
            if n == intentos:
                raise
            time.sleep(5 * n)


# ---------- proceso ----------

def en_zona(lat, lon, localidad, radio):
    dist = None
    if lat is not None and lon is not None:
        dist = min(km(lat, lon, a, b) for a, b in CENTROS.values())
    ok = (dist is not None and dist <= radio) or norm(localidad) in LOCALIDADES
    return ok, dist


def procesar(ruta_zip, args):
    filtro = [norm(c) for c in args.cadenas.split(",") if c.strip()]
    sucursales, catalogo, precios = {}, {}, {}
    n_promos = 0

    for zf in comercios(ruta_zip):
        n_com, n_suc, n_pro = buscar(zf, "comercio.csv"), buscar(zf, "sucursales.csv"), buscar(zf, "productos.csv")
        if not (n_com and n_suc and n_pro):
            continue

        banderas, razon, url = {}, "", ""
        it = filas(zf, n_com)
        cols = next(it)
        i_id, i_nom, i_raz, i_url = ix(cols, "id_bandera"), ix(cols, "bandera_nombre"), ix(cols, "razon_social"), ix(cols, "bandera_url")
        for f in it:
            banderas[v(f, i_id)] = v(f, i_nom)
            razon = razon or v(f, i_raz)
            url = url or v(f, i_url)

        propias = {}
        it = filas(zf, n_suc)
        cols = next(it)
        i_c, i_b, i_s = ix(cols, "id_comercio"), ix(cols, "id_bandera"), ix(cols, "id_sucursal")
        i_nom, i_cal, i_num, i_loc = ix(cols, "nombre"), ix(cols, "calle"), ix(cols, "numero"), ix(cols, "localidad")
        i_lat, i_lon = ix(cols, "latitud"), ix(cols, "longitud")
        for f in it:
            lat, lon = numero(v(f, i_lat)), numero(v(f, i_lon))
            ok, dist = en_zona(lat, lon, v(f, i_loc), args.radio_km)
            if not ok:
                continue
            cadena = banderas.get(v(f, i_b)) or razon or url
            if filtro and not any(x in norm(f"{cadena} {razon} {url}") for x in filtro):
                continue
            clave = (v(f, i_c), v(f, i_b), v(f, i_s))
            propias[clave] = cadena
            sucursales["-".join(clave)] = {
                "cadena": cadena, "razon_social": razon, "nombre": v(f, i_nom),
                "direccion": f"{v(f, i_cal)} {v(f, i_num)}, {v(f, i_loc)}".strip(" ,"),
                "lat": lat, "lon": lon,
                "dist_km": round(dist, 2) if dist is not None else None,
            }

        if args.listar or not propias:
            continue

        it = filas(zf, n_pro)
        cols = next(it)
        i_c, i_b, i_s = ix(cols, "id_comercio"), ix(cols, "id_bandera"), ix(cols, "id_sucursal")
        i_ean, i_des, i_mar = ix(cols, "ean"), ix(cols, "descripcion"), ix(cols, "marca")
        i_can, i_uni = ix(cols, "cantidad_presentacion"), ix(cols, "unidad_medida_presentacion")
        i_lis, i_pro, i_ley = ix(cols, "precio_lista"), ix(cols, "precio_unitario_promo1"), ix(cols, "leyenda_promo1")
        for f in it:
            cadena = propias.get((v(f, i_c), v(f, i_b), v(f, i_s)))
            if not cadena:
                continue
            ean = re.sub(r"\D", "", v(f, i_ean))
            lista = numero(v(f, i_lis))
            if len(ean) < 8 or not lista or lista <= 0:
                continue
            promo, leyenda = numero(v(f, i_pro)), v(f, i_ley)
            if not promo or promo <= 0 or promo >= lista or promo < lista * (1 - DESCUENTO_MAX):
                promo, leyenda = None, ""
            catalogo.setdefault(ean, [v(f, i_des), v(f, i_mar), f"{v(f, i_can)} {v(f, i_uni)}".strip()])
            por_cadena = precios.setdefault(ean, {})
            actual = por_cadena.get(cadena)
            if actual is None:
                por_cadena[cadena] = [round(lista, 2), round(promo, 2) if promo else None, leyenda]
                n_promos += 1 if promo else 0
            else:  # varias sucursales de la misma cadena: me quedo con lo mas barato
                actual[0] = min(actual[0], round(lista, 2))
                if promo and (actual[1] is None or promo < actual[1]):
                    actual[1], actual[2] = round(promo, 2), leyenda
    return sucursales, catalogo, precios, n_promos


def guardar(carpeta, nombre, datos):
    ruta = os.path.join(carpeta, nombre)
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump(datos, f, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    print(f"  {nombre}: {os.path.getsize(ruta) / 1e6:.2f} MB")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--salida", default="salida")
    ap.add_argument("--fecha", help="AAAA-MM-DD (por defecto, el archivo mas reciente)")
    ap.add_argument("--zip", help="usar un zip SEPA local en vez de bajarlo")
    ap.add_argument("--radio-km", type=float, default=4.0, help="radio alrededor de cada centro (default 4)")
    ap.add_argument("--cadenas", default="", help="filtro opcional, separado por comas (ej: coto,carrefour,disco)")
    ap.add_argument("--listar", action="store_true", help="solo lista las sucursales de la zona y termina")
    args = ap.parse_args()

    temporal = None
    if args.zip:
        ruta = args.zip
        fecha = fecha_del_zip(ruta)
    else:
        fecha, url = elegir_recurso(args.fecha)
        print(f"Bajando archivo SEPA del {fecha} ...")
        temporal = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
        temporal.close()
        ruta = temporal.name
        bajar(url, ruta)

    try:
        print("Procesando ...")
        sucursales, catalogo, precios, n_promos = procesar(ruta, args)
    finally:
        if temporal:
            os.remove(temporal.name)

    por_cadena = {}
    for s in sucursales.values():
        por_cadena.setdefault(s["cadena"], []).append(s)
    print(f"\nSucursales en la zona: {len(sucursales)}")
    for cadena, lista in sorted(por_cadena.items()):
        print(f"\n{cadena}  ({lista[0]['razon_social']})  - {len(lista)} sucursal(es)")
        for s in sorted(lista, key=lambda x: x["dist_km"] if x["dist_km"] is not None else 99):
            print(f"   {s['direccion']}  [{s['dist_km']} km]")
    if args.listar:
        return

    os.makedirs(args.salida, exist_ok=True)
    print(f"\nProductos: {len(catalogo)} | con promo: {n_promos}")
    guardar(args.salida, "sucursales.json", sucursales)
    guardar(args.salida, "catalogo.json", catalogo)
    guardar(args.salida, "precios.json", precios)
    guardar(args.salida, "indice.json", {
        "fecha_datos": fecha,
        "generado": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "cadenas": {c: len(l) for c, l in sorted(por_cadena.items())},
        "productos": len(catalogo),
        "productos_en_mas_de_una_cadena": sum(1 for p in precios.values() if len(p) > 1),
        "precios_con_promo": n_promos,
        "fuente": "Precios Claros - Base SEPA, Secretaria de Comercio Interior (datos.produccion.gob.ar), CC BY 4.0. "
                  "Datos filtrados y reorganizados; no es una publicacion oficial.",
    })


if __name__ == "__main__":
    main()
