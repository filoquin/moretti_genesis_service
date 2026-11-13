"""Configuracion: archivo .ini (mismo formato y claves que genesis-proxy.ini) y variables de entorno.

Prioridad: linea de comandos > variables de entorno > .ini > valores por defecto.
"""
import os
from dataclasses import dataclass

# clave del .ini -> variable de entorno
ENTORNO = {
    "puerto_serie": "FISCAL_SERIAL_PORT",
    "baudios": "FISCAL_BAUDRATE",
    "escuchar": "FLASK_HOST",
    "puerto_http": "FLASK_PORT",
    "simular": "FISCAL_SIMULAR",
    "abrir_cajon": "FISCAL_ABRIR_CAJON",
    "log_detallado": "FISCAL_LOG_DETALLADO",
    "carpeta_datos": "FISCAL_CARPETA_DATOS",
    "secuencia_incremental": "FISCAL_SECUENCIA_INCREMENTAL",
    "clave_firma": "FISCAL_CLAVE_FIRMA",
    "firma_ventana_seg": "FISCAL_FIRMA_VENTANA_SEG",
    "origenes": "FISCAL_ORIGENES",
}


def _carpeta_por_defecto() -> str:
    base = os.environ.get("PROGRAMDATA")
    if base:
        return os.path.join(base, "GenesisProxy")
    return os.path.join(os.path.expanduser("~"), ".genesis-proxy")


def leer_ini(archivo: str) -> dict:
    d = {}
    if not archivo or not os.path.exists(archivo):
        return d
    with open(archivo, encoding="utf-8-sig") as f:
        for cruda in f:
            linea = cruda.strip()
            if not linea or linea[0] in "#;[":
                continue
            i = linea.find("=")
            if i <= 0:
                continue
            d[linea[:i].strip().lower()] = linea[i + 1:].strip()
    return d


def si(v: str) -> bool:
    return (v or "").strip().lower() in ("si", "s", "true", "1", "yes")


@dataclass
class Config:
    puerto_serie: str = "auto"
    baudios: int = 115200
    escuchar: str = "127.0.0.1"
    puerto_http: int = 5005
    simular: bool = False
    abrir_cajon: bool = True
    log_detallado: bool = True
    carpeta_datos: str = ""
    secuencia_incremental: bool = False
    clave_firma: str = ""
    firma_ventana_seg: int = 120
    origenes: str = ""
    archivo: str = ""

    @classmethod
    def cargar(cls, archivo: str) -> "Config":
        ini = leer_ini(archivo)

        def valor(clave: str, defecto: str) -> str:
            v = os.environ.get(ENTORNO[clave], "") or ini.get(clave, "")
            return v if v else defecto

        return cls(
            puerto_serie=valor("puerto_serie", "auto"),
            baudios=int(valor("baudios", "115200")),
            escuchar=valor("escuchar", "127.0.0.1"),
            puerto_http=int(valor("puerto_http", "5005")),
            simular=si(valor("simular", "no")),
            abrir_cajon=si(valor("abrir_cajon", "si")),
            log_detallado=si(valor("log_detallado", "si")),
            carpeta_datos=valor("carpeta_datos", _carpeta_por_defecto()),
            secuencia_incremental=si(valor("secuencia_incremental", "no")),
            clave_firma=valor("clave_firma", ""),
            firma_ventana_seg=int(valor("firma_ventana_seg", "120")),
            origenes=valor("origenes", ""),
            archivo=archivo if archivo and os.path.exists(archivo) else "",
        )
