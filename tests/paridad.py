"""Compara este servicio con genesis-fiscal (C#), los dos en modo simulado.

Manda los mismos pedidos (tests/casos_paridad.py) a los dos y compara, caso por caso:
- los comandos que cada uno le mando al impresor (lineas "SIM ..." del log detallado);
- la respuesta JSON (con la fecha y hora normalizadas).

Uso:
  python tests/paridad.py URL_CSHARP LOG_CSHARP URL_PYTHON LOG_PYTHON

Los dos servicios tienen que arrancar con la carpeta de datos vacia, simular = si y log_detallado = si.
Solo usa la biblioteca estandar.
"""
import json
import os
import re
import sys
import urllib.request
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(__file__))
from casos_paridad import CASOS  # noqa: E402

FECHA = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")


def url_de(base, ruta, datos):
    if ruta == "/print_pos_ticket":
        forzar = isinstance(datos, tuple)
        if forzar:
            datos = datos[1]
        vals = datos if isinstance(datos, str) else json.dumps(datos, ensure_ascii=False)
        q = "vals=" + quote(vals, safe="") + ("&forzar=1" if forzar else "")
    else:
        q = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in datos.items())
    return base.rstrip("/") + ruta + ("?" + q if q else "")


def pedir(url):
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def normalizar(o):
    if isinstance(o, dict):
        return {k: normalizar(v) for k, v in o.items()}
    if isinstance(o, list):
        return [normalizar(v) for v in o]
    if isinstance(o, bool) or o is None:
        return o
    if isinstance(o, (int, float)):
        return round(float(o), 4)
    if isinstance(o, str):
        # El detalle del error de JSON invalido lo da cada biblioteca: se compara solo el comienzo.
        if o.startswith("Los valores del tique no son un JSON valido:"):
            return "Los valores del tique no son un JSON valido: <detalle>"
        return FECHA.sub("<fecha>", o)
    return o


class Log:
    def __init__(self, ruta):
        self.ruta, self.pos = ruta, 0

    def nuevas_sim(self):
        with open(self.ruta, encoding="utf-8") as f:
            f.seek(self.pos)
            texto = f.read()
            self.pos = f.tell()
        return [l.split(" SIM ", 1)[1] for l in texto.splitlines() if " SIM " in l]


def main(url_cs, log_cs, url_py, log_py):
    lc, lp = Log(log_cs), Log(log_py)
    lc.nuevas_sim(), lp.nuevas_sim()
    fallas = 0
    for nombre, ruta, datos in CASOS:
        rc = pedir(url_de(url_cs, ruta, datos))
        rp = pedir(url_de(url_py, ruta, datos))
        sc, sp = lc.nuevas_sim(), lp.nuevas_sim()
        rpn = normalizar(rp)
        rpn.pop("ok", None)
        problemas = []
        if sc != sp:
            problemas.append("comandos distintos")
        if normalizar(rc) != rpn:
            problemas.append("respuesta distinta")
        estado = "OK " if not problemas else "MAL"
        print(f"{estado} {nombre}: {len(sc)} comandos; {str(rc.get('response'))[:70]}")
        if problemas:
            fallas += 1
            print("    C#    :", json.dumps(rc, ensure_ascii=False))
            print("    Python:", json.dumps(rp, ensure_ascii=False))
            for i in range(max(len(sc), len(sp))):
                a = sc[i] if i < len(sc) else "-"
                b = sp[i] if i < len(sp) else "-"
                if a != b:
                    print(f"    [{i}] C#    : {a}\n    [{i}] Python: {b}")
    print(f"\n{len(CASOS) - fallas} de {len(CASOS)} casos iguales")
    return 1 if fallas else 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:5]))
