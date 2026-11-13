"""API HTTP del servicio.

Dos juegos de rutas sobre el mismo motor:

- API JSON (POST, cuerpo JSON): /imprimir_ticket, /cierre_x, /cierre_z. Respuesta {"ok": ..., ...} con
  codigo HTTP segun el resultado. Firma en cabeceras.
- Rutas compatibles con pos_proxy_service y genesis-fiscal (GET): /print_pos_ticket?vals=...,
  /print_pos_fiscal_close?type=..., /state_printer. Respuesta {"response": true | "<error>"} siempre con
  HTTP 200 salvo seguridad (403). Firma en la consulta. Sirven para usar el servicio sin tocar Odoo.

/health y /estado no piden firma.
"""
import json
from datetime import datetime
from decimal import Decimal
from typing import Optional

from flask import Flask, Response, request

from . import registro
from .comprobante import Comprobante
from .genesis import Genesis, ResultadoEmision
from .protocolo import ErrorComunicacion, ErrorDuplicado, ErrorImpresor, ErrorPedido
from .seguridad import Seguridad

SERVICIO = "moretti-genesis-service"
VERSION = "0.2.0"

RUTAS_FIRMADAS_GET = {"/print_pos_ticket", "/print_pos_fiscal_close"}
RUTAS_FIRMADAS_JSON = {"/imprimir_ticket", "/cierre_x", "/cierre_z"}


def _json(datos, codigo: int = 200) -> Response:
    def conv(o):
        if isinstance(o, Decimal):
            return float(o)
        raise TypeError(type(o).__name__)
    return Response(json.dumps(datos, ensure_ascii=False, default=conv), status=codigo,
                    mimetype="application/json")


def _ruta() -> str:
    return request.path.rstrip("/").lower() or "/"


def _comprobante_dict(r: ResultadoEmision, simulado: bool) -> dict:
    return {"numero": r.numero, "letra": r.letra, "tipo": r.tipo, "total": r.total, "iva": r.iva,
            "vuelto": r.vuelto, "simulado": simulado}


def crear_app(genesis: Genesis, seguridad: Seguridad, simulado: bool) -> Flask:
    app = Flask(__name__)
    app.url_map.strict_slashes = False
    estado = {"inicio": datetime.now(), "ultimo": ""}

    def ahora() -> str:
        return datetime.now().strftime("%H:%M:%S")

    def anotar_error(ex: Exception) -> None:
        estado["ultimo"] = f"{ahora()} {ex}"
        if isinstance(ex, ErrorComunicacion):
            registro.log("ERROR de comunicacion: " + str(ex))

    # --- seguridad y CORS --------------------------------------------------------------------

    @app.before_request
    def antes():
        origen = request.headers.get("Origin")
        if not seguridad.origen_aceptado(origen):
            registro.log(f"Rechazado: origen no permitido {origen} ({request.method} {request.path})")
            return _json({"ok": False, "response": f"Origen no permitido: {origen}",
                          "error": f"Origen no permitido: {origen}", "categoria": "seguridad"}, 403)
        if request.method == "OPTIONS":
            return _json({"response": True, "ok": True})
        ruta = _ruta()
        motivo = None
        if request.method == "GET" and ruta in RUTAS_FIRMADAS_GET:
            motivo = seguridad.controlar_firma_consulta(ruta, request.query_string.decode("utf-8", "replace"))
        elif request.method == "POST" and ruta in RUTAS_FIRMADAS_JSON:
            motivo = seguridad.controlar_firma_json(ruta, request.headers.get("X-Genesis-Ts"),
                                                    request.headers.get("X-Genesis-Firma"), request.get_data())
        if motivo:
            registro.log(f"Rechazado {ruta}: {motivo}" + (f" (origen {origen})" if origen else ""))
            return _json({"ok": False, "response": motivo, "error": motivo, "categoria": "seguridad"}, 403)
        return None

    @app.after_request
    def cors(resp: Response):
        permitir = seguridad.permitir_origen(request.headers.get("Origin"))
        if permitir is not None and resp.status_code != 403:
            resp.headers["Access-Control-Allow-Origin"] = permitir
            if permitir != "*":
                resp.headers["Vary"] = "Origin"
            resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Genesis-Ts, X-Genesis-Firma"
            resp.headers["Access-Control-Allow-Private-Network"] = "true"
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.errorhandler(404)
    def no_encontrada(_e):
        return _json({"ok": False, "response": "Ruta desconocida: " + _ruta(), "error": "Ruta desconocida"}, 404)

    @app.errorhandler(405)
    def metodo(_e):
        return _json({"ok": False, "response": "Metodo no permitido", "error": "Metodo no permitido"}, 405)

    # --- API JSON ----------------------------------------------------------------------------

    def error_json(ex: Exception):
        anotar_error(ex)
        if isinstance(ex, ErrorDuplicado):
            return _json({"ok": False, "error": str(ex), "categoria": "duplicado"}, 409)
        if isinstance(ex, ErrorPedido):
            return _json({"ok": False, "error": str(ex), "categoria": "pedido"}, 400)
        if isinstance(ex, ErrorImpresor):
            return _json({"ok": False, "error": str(ex), "categoria": "impresor",
                          "codigo_impresor": f"{ex.codigo:04X}"}, 422)
        if isinstance(ex, ErrorComunicacion):
            return _json({"ok": False, "error": str(ex), "categoria": "comunicacion"}, 503)
        registro.log(f"ERROR inesperado en {_ruta()}: {ex!r}")
        return _json({"ok": False, "error": "Error interno del servicio fiscal: " + str(ex), "categoria": "interno"}, 500)

    @app.post("/imprimir_ticket")
    def imprimir_ticket():
        try:
            cuerpo = request.get_data()
            try:
                datos = json.loads(cuerpo or b"null", parse_float=Decimal)
            except ValueError as ex:
                raise ErrorPedido("El cuerpo no es un JSON valido: " + str(ex)) from ex
            forzar = isinstance(datos, dict) and datos.get("forzar") is True
            c = Comprobante.desde_dict(datos)
            r = genesis.emitir(c, forzar)
            estado["ultimo"] = f"{ahora()} emitido {(r.letra + ' ' + r.numero).strip()}"
            return _json({"ok": True, "comprobante": _comprobante_dict(r, simulado)})
        except Exception as ex:  # noqa: BLE001
            return error_json(ex)

    def cierre_json(tipo: str):
        try:
            numero = genesis.cierre(tipo)
            estado["ultimo"] = f"{ahora()} cierre {tipo} {numero}"
            return _json({"ok": True, "tipo": tipo, "numero": numero})
        except Exception as ex:  # noqa: BLE001
            return error_json(ex)

    @app.post("/cierre_x")
    def cierre_x():
        return cierre_json("x")

    @app.post("/cierre_z")
    def cierre_z():
        return cierre_json("z")

    @app.get("/health")
    def health():
        return _json({"ok": True, "service": SERVICIO, "version": VERSION, "simulado": simulado})

    @app.get("/estado")
    def ver_estado():
        extra = {"servicio": f"{SERVICIO} {VERSION}", "simulado": simulado, "canal": genesis.canal,
                 "iniciado": estado["inicio"].strftime("%Y-%m-%d %H:%M:%S"),
                 "pedidos_registrados": registro.cantidad(), "ultimo": estado["ultimo"],
                 "firma_exigida": seguridad.exige_firma}
        try:
            r = genesis.estado()
            return _json({"ok": True, "response": True, **extra,
                          "estado_impresora": f"{r.estado_impresora:04X}", "estado_fiscal": f"{r.estado_fiscal:04X}"})
        except Exception as ex:  # noqa: BLE001
            return _json({"ok": False, "response": str(ex), "error": str(ex), **extra})

    # --- rutas compatibles con pos_proxy_service ---------------------------------------------

    def respuesta_compat(fn):
        try:
            return _json(fn())
        except (ErrorPedido, ErrorImpresor, ErrorComunicacion) as ex:
            anotar_error(ex)
            return _json({"response": str(ex)})
        except Exception as ex:  # noqa: BLE001
            registro.log(f"ERROR inesperado en {_ruta()}: {ex!r}")
            return _json({"response": "Error interno del proxy fiscal: " + str(ex)})

    @app.get("/print_pos_ticket")
    def print_pos_ticket():
        def hacer():
            vals = request.args.get("vals", "")
            if not vals.strip():
                return {"response": "Debe enviar los valores del tiquet"}
            r = genesis.emitir(Comprobante.desde_json(vals), request.args.get("forzar") == "1")
            estado["ultimo"] = f"{ahora()} emitido {(r.letra + ' ' + r.numero).strip()}"
            return {"response": True, "comprobante": _comprobante_dict(r, simulado)}
        return respuesta_compat(hacer)

    @app.get("/print_pos_fiscal_close")
    def print_pos_fiscal_close():
        def hacer():
            tipo: Optional[str] = request.args.get("type")
            if tipo is None:
                return {"response": "Debe enviar el tipo de cierre fiscal"}
            numero = genesis.cierre(tipo)
            estado["ultimo"] = f"{ahora()} cierre {tipo} {numero}"
            return {"response": True, "numero": numero}
        return respuesta_compat(hacer)

    @app.get("/state_printer")
    def state_printer():
        def hacer():
            genesis.estado()
            return {"response": True}
        return respuesta_compat(hacer)

    return app
