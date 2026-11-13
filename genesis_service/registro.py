"""Log del servicio y registro de comprobantes emitidos (para no emitir dos veces el mismo pedido).

Los archivos tienen el mismo formato que genesis-fiscal (Registro.cs): se pueden mezclar.
"""
import json
import os
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Dict, Optional

_candado = threading.RLock()
_carpeta = "."
_detalle = True
_simulado = False
_emitidos: Dict[str, "Emitido"] = {}


@dataclass
class Emitido:
    Nombre: str = ""
    Estado: str = "emitido"  # emitido | incierto
    Numero: str = ""
    Letra: str = ""
    Total: str = ""
    Tipo: str = ""
    Fecha: str = ""


def iniciar(carpeta: str, con_detalle: bool, simulado: bool) -> None:
    global _carpeta, _detalle, _simulado
    _carpeta, _detalle, _simulado = carpeta, con_detalle, simulado
    os.makedirs(carpeta, exist_ok=True)
    _emitidos.clear()
    hoy = datetime.now()
    _cargar(hoy.replace(day=1) - timedelta(days=1))
    _cargar(hoy)


def carpeta() -> str:
    return _carpeta


def _archivo_log() -> str:
    return os.path.join(_carpeta, "genesis-proxy-simulado.log" if _simulado else "genesis-proxy.log")


def _archivo_emitidos(mes: datetime) -> str:
    return os.path.join(_carpeta, ("emitidos-simulado-" if _simulado else "emitidos-") + mes.strftime("%Y-%m") + ".jsonl")


def log(mensaje: str) -> None:
    linea = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + " " + mensaje
    with _candado:
        try:
            arch = _archivo_log()
            if os.path.exists(arch) and os.path.getsize(arch) > 5 * 1024 * 1024:
                viejo = arch + ".1"
                if os.path.exists(viejo):
                    os.remove(viejo)
                os.replace(arch, viejo)
            with open(arch, "a", encoding="utf-8", newline="") as f:
                f.write(linea + os.linesep)
        except OSError:
            pass
    try:
        print(linea, flush=True)
    except Exception:
        pass


def detalle(mensaje: str) -> None:
    """Tramas completas: sirven para diagnosticar, pero incluyen datos de ventas."""
    if _detalle:
        log("    " + mensaje)


def _cargar(mes: datetime) -> None:
    arch = _archivo_emitidos(mes)
    if not os.path.exists(arch):
        return
    with open(arch, encoding="utf-8-sig") as f:
        for linea in f:
            if not linea.strip():
                continue
            try:
                d = json.loads(linea)
                e = Emitido(**{k: str(d.get(k, "") or "") for k in Emitido.__dataclass_fields__})
                if e.Estado == "":
                    e.Estado = "emitido"
                if e.Nombre:
                    _emitidos[e.Nombre.lower()] = e
            except (ValueError, TypeError):
                pass


def buscar(nombre: str) -> Optional[Emitido]:
    if not nombre:
        return None
    with _candado:
        return _emitidos.get(nombre.lower())


def anotar(e: Emitido) -> None:
    e.Fecha = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _candado:
        if e.Nombre:
            _emitidos[e.Nombre.lower()] = e
        try:
            with open(_archivo_emitidos(datetime.now()), "a", encoding="utf-8", newline="") as f:
                f.write(json.dumps(asdict(e), ensure_ascii=False) + os.linesep)
        except OSError as ex:
            try:
                print("No se pudo guardar el registro de emitidos: " + str(ex), flush=True)
            except Exception:
                pass


def cantidad() -> int:
    with _candado:
        return len(_emitidos)
