"""Quien puede pedirle algo al servicio. Portado de genesis-fiscal (Seguridad.cs).

Dos controles, independientes y configurables:

1. Origin (`origenes`): si el pedido trae cabecera Origin y no esta en la lista, se rechaza. Un pedido
   sin Origin (curl, abrir la URL a mano) pasa: es para cortar paginas ajenas abiertas en la caja.

2. Firma (`clave_firma`): los pedidos que emiten algo tienen que venir firmados con HMAC-SHA256.

   API JSON (POST), firma en cabeceras:
       X-Genesis-Ts:    segundos Unix (UTC)
       X-Genesis-Firma: hex(HMAC_SHA256(clave, "POST " + ruta + "\n" + ts + "\n" + cuerpo))
   donde cuerpo son los bytes exactos del cuerpo del pedido.

   Rutas compatibles con pos_proxy_service (GET), firma en la consulta (igual que genesis-fiscal):
       firma = hex(HMAC_SHA256(clave, ruta + "?" + consulta_sin_firma))   con ts dentro de la consulta
       URL   = ruta + "?" + consulta_sin_firma + "&firma=" + firma

   Se rechaza si la firma no coincide, si ts esta a mas de `firma_ventana_seg` de la hora de esta PC o
   si esa misma firma ya se uso.
"""
import hashlib
import hmac
import threading
import time
from typing import Optional


class Seguridad:
    def __init__(self, clave: str = "", origenes: str = "", ventana_seg: int = 120):
        clave = (clave or "").strip()
        self._clave = clave.encode("utf-8") if clave else None
        self._origenes = {o.strip().rstrip("/").lower()
                          for o in (origenes or "").replace(";", ",").replace(" ", ",").split(",") if o.strip()}
        self.ventana_seg = ventana_seg if ventana_seg > 0 else 120
        self._usadas = {}
        self._candado = threading.Lock()

    @property
    def exige_firma(self) -> bool:
        return self._clave is not None

    @property
    def controla_origen(self) -> bool:
        return bool(self._origenes)

    @property
    def origenes(self) -> str:
        return ", ".join(sorted(self._origenes))

    def origen_aceptado(self, origen: Optional[str]) -> bool:
        if origen is None or not self.controla_origen:
            return True
        return origen.strip().rstrip("/").lower() in self._origenes

    def permitir_origen(self, origen: Optional[str]) -> Optional[str]:
        """Valor para Access-Control-Allow-Origin: el Origin aceptado, o * si no hay lista."""
        if not self.controla_origen:
            return "*"
        return origen.strip() if origen is not None and self.origen_aceptado(origen) else None

    # --- firma -------------------------------------------------------------------------------

    def controlar_firma_json(self, ruta: str, ts: Optional[str], firma: Optional[str], cuerpo: bytes) -> Optional[str]:
        """Devuelve None si el pedido es valido, o el motivo del rechazo."""
        if self._clave is None:
            return None
        if not firma:
            return "Pedido sin firma (cabecera X-Genesis-Firma)"
        mensaje = ("POST " + ruta + "\n" + (ts or "") + "\n").encode("utf-8") + (cuerpo or b"")
        return self._controlar(ts, firma, mensaje)

    def controlar_firma_consulta(self, ruta: str, consulta: str) -> Optional[str]:
        if self._clave is None:
            return None
        marca = "&firma="
        i = consulta.rfind(marca)
        if i < 0:
            return "Pedido sin firma"
        firmada, firma = consulta[:i], consulta[i + len(marca):]
        ts = None
        for par in firmada.split("&"):
            if par.startswith("ts="):
                ts = par[3:]
        return self._controlar(ts, firma, (ruta + "?" + firmada).encode("utf-8"))

    def _controlar(self, ts: Optional[str], firma: str, mensaje: bytes) -> Optional[str]:
        if not ts or not ts.isdigit() or not ts.isascii():
            return "Pedido sin hora (ts)"
        desfase = int(ts) - int(time.time())
        if abs(desfase) > self.ventana_seg:
            return f"Pedido vencido o reloj desfasado entre el navegador y esta PC ({desfase} s)"
        esperada = hmac.new(self._clave, mensaje, hashlib.sha256).hexdigest()
        firma = firma.strip().lower()
        if not hmac.compare_digest(esperada, firma):
            return "Firma invalida"
        with self._candado:
            limite = time.time() - 2 * self.ventana_seg
            for k in [k for k, t in self._usadas.items() if t < limite]:
                del self._usadas[k]
            if firma in self._usadas:
                return "Pedido repetido (esa firma ya se uso)"
            self._usadas[firma] = time.time()
        return None
