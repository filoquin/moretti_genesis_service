"""Pruebas del servicio en modo simulado y del transporte serie con un puerto falso.

    python -m pytest tests
"""
import hashlib
import hmac
import json
import os
import sys
import time
from urllib.parse import quote

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from genesis_service import protocolo as P  # noqa: E402
from genesis_service import registro  # noqa: E402
from genesis_service.genesis import Genesis  # noqa: E402
from genesis_service.http import crear_app  # noqa: E402
from genesis_service.seguridad import Seguridad  # noqa: E402
from genesis_service.transporte import TransporteSerie, TransporteSimulado  # noqa: E402

CLAVE = "3f9a1c0d5e7b2a4c6d8e0f1a2b3c4d5e"
GORO = "https://www.casagoro.com.ar"


def ticket(name="Shop/0001", typ=83, items=None, pagos=None, cliente=None):
    return {"name": name, "type": typ, "cliente": cliente or {},
            "items": items or [{"description": "Mate", "qty": 1, "price": 5500, "iva": 21}],
            "pagos": pagos or [{"codigo_forma_pago": 8, "monto": 6000, "descripcion": "Efectivo"}],
            "descuentos": [], "ajustes": []}


@pytest.fixture
def servicio(tmp_path):
    registro.iniciar(str(tmp_path), True, True)
    sim = TransporteSimulado()
    genesis = Genesis(sim, True)
    seg = Seguridad(CLAVE, f"{GORO}, https://test19.casagoro.com.ar", 120)
    app = crear_app(genesis, seg, True)
    return app.test_client(), sim


def firmar_json(ruta, cuerpo: bytes, ts=None):
    ts = str(int(time.time()) if ts is None else ts)
    f = hmac.new(CLAVE.encode(), ("POST " + ruta + "\n" + ts + "\n").encode() + cuerpo, hashlib.sha256).hexdigest()
    return {"X-Genesis-Ts": ts, "X-Genesis-Firma": f, "Content-Type": "application/json", "Origin": GORO}


def url_firmada(ruta, params, ts=None):
    q = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in params.items())
    q += ("&" if q else "") + f"ts={int(time.time()) if ts is None else ts}"
    f = hmac.new(CLAVE.encode(), (ruta + "?" + q).encode(), hashlib.sha256).hexdigest()
    return ruta + "?" + q + "&firma=" + f


# --- protocolo --------------------------------------------------------------------------------

def test_vector_checksum():
    assert P.armar(0x0B0B, 0, None) == bytes.fromhex("02A90B0B1C000003") + b"00E0"


def test_respuesta_con_error():
    r = P.interpretar(bytes.fromhex("A900001CC0821C1C01011C"))
    assert r.error == 0x0101 and not r.ok and r.campos == []


# --- API JSON ---------------------------------------------------------------------------------

def test_json_emite_y_rechaza_duplicado(servicio):
    cli, sim = servicio
    cuerpo = json.dumps(ticket()).encode()
    r = cli.post("/imprimir_ticket", data=cuerpo, headers=firmar_json("/imprimir_ticket", cuerpo))
    assert r.status_code == 200, r.json
    assert r.json["ok"] is True and r.json["comprobante"]["numero"] == "00900001"
    assert r.json["comprobante"]["total"] == 5500 and r.json["comprobante"]["vuelto"] == 500
    assert r.headers["Access-Control-Allow-Origin"] == GORO

    cuerpo2 = json.dumps(ticket()).encode() + b" "  # otra firma, mismo pedido
    r = cli.post("/imprimir_ticket", data=cuerpo2, headers=firmar_json("/imprimir_ticket", cuerpo2))
    assert r.status_code == 409 and r.json["categoria"] == "duplicado"

    cuerpo3 = json.dumps(dict(ticket(), forzar=True)).encode()
    r = cli.post("/imprimir_ticket", data=cuerpo3, headers=firmar_json("/imprimir_ticket", cuerpo3))
    assert r.status_code == 200 and r.json["comprobante"]["numero"] == "00900002"


def test_json_errores(servicio):
    cli, _ = servicio
    for cuerpo, codigo, categoria in [
        (json.dumps(ticket(typ=99)).encode(), 400, "pedido"),
        (b"{no es json", 400, "pedido"),
        (json.dumps(ticket(name="Shop/9", typ=82, cliente={"numero_documento": "20123456"},
                           items=[{"description": "x", "qty": -1, "price": 10, "iva": 21}])).encode(), 400, "pedido"),
    ]:
        r = cli.post("/imprimir_ticket", data=cuerpo, headers=firmar_json("/imprimir_ticket", cuerpo))
        assert (r.status_code, r.json["categoria"]) == (codigo, categoria), r.json


def test_json_cierres(servicio):
    cli, _ = servicio
    r = cli.post("/cierre_x", data=b"", headers=firmar_json("/cierre_x", b""))
    assert r.json == {"ok": True, "tipo": "x", "numero": "00012"}
    r = cli.post("/cierre_z", data=b"", headers=firmar_json("/cierre_z", b""))
    assert r.json["numero"] == "00091"


def test_json_seguridad(servicio):
    cli, sim = servicio
    cuerpo = json.dumps(ticket(name="Shop/S")).encode()
    h = firmar_json("/imprimir_ticket", cuerpo)
    casos = [
        ({k: v for k, v in h.items() if k != "X-Genesis-Firma"}, cuerpo, "Pedido sin firma"),
        (dict(h, **{"X-Genesis-Firma": "0" * 64}), cuerpo, "Firma invalida"),
        (h, cuerpo.replace(b"5500", b"1"), "Firma invalida"),               # cuerpo cambiado
        (firmar_json("/imprimir_ticket", cuerpo, ts=int(time.time()) - 600), cuerpo, "vencido"),
        (dict(h, Origin="https://malo.example"), cuerpo, "Origen no permitido"),
    ]
    for headers, data, motivo in casos:
        r = cli.post("/imprimir_ticket", data=data, headers=headers)
        assert r.status_code == 403 and motivo in r.json["error"], (motivo, r.json)
    # una firma de cierre X no sirve para Z
    hx = firmar_json("/cierre_x", b"")
    assert cli.post("/cierre_z", data=b"", headers=hx).status_code == 403
    assert not sim.historial  # nada llego al impresor
    assert cli.post("/imprimir_ticket", data=cuerpo, headers=h).status_code == 200
    assert "repetido" in cli.post("/imprimir_ticket", data=cuerpo, headers=h).json["error"]


# --- rutas compatibles (GET) ------------------------------------------------------------------

def test_compat_firmada(servicio):
    cli, _ = servicio
    vals = json.dumps(ticket(name="Shop/G"))
    r = cli.get(url_firmada("/print_pos_ticket", {"vals": vals}), headers={"Origin": GORO})
    assert r.status_code == 200 and r.json["response"] is True
    r = cli.get("/print_pos_fiscal_close?type=z")  # el <img> de una pagina ajena
    assert r.status_code == 403 and r.json["response"] == "Pedido sin firma"
    x = url_firmada("/print_pos_fiscal_close", {"type": "x"})
    assert cli.get(x.replace("type=x", "type=z")).status_code == 403
    assert cli.get("/state_printer").json == {"response": True}
    assert cli.get("/estado").json["response"] is True
    assert cli.options("/print_pos_ticket", headers={"Origin": "https://malo.example"}).status_code == 403


def test_compat_errores_con_200(servicio):
    cli, _ = servicio
    r = cli.get(url_firmada("/print_pos_ticket", {"vals": json.dumps(ticket(typ=99))}))
    assert r.status_code == 200 and r.json["response"].startswith("Tipo de comprobante no soportado")


# --- cambio respecto de genesis-fiscal: numero de pedido recortado ----------------------------

def test_nombre_largo_se_recorta(servicio):
    cli, sim = servicio
    nombre = "Juguetería Alberdi Caja 5/000123-456-7890"
    vals = json.dumps(ticket(name=nombre))
    assert cli.get(url_firmada("/print_pos_ticket", {"vals": vals})).json["response"] is True
    item = next(l for l in sim.historial if l.startswith("0A02"))
    primera = item.split("|")[1]
    assert primera == P.recortar(nombre, 28) and len(primera) <= 28


# --- transporte serie con un puerto falso ------------------------------------------------------

class PuertoFalso:
    """Devuelve las respuestas programadas, una por cada trama escrita."""

    def __init__(self, respuestas):
        self.respuestas = list(respuestas)
        self.escrito = []
        self.entrada = bytearray()
        self.is_open = True

    def write(self, b):
        self.escrito.append(bytes(b))
        if len(b) > 1 and b[0] == P.STX and self.respuestas:
            self.entrada.extend(self.respuestas.pop(0))

    def read(self, n):
        if not self.entrada:
            time.sleep(0.001)
            return b""
        b, self.entrada = bytes(self.entrada[:n]), self.entrada[n:]
        return b

    def reset_input_buffer(self):
        self.entrada.clear()

    def flush(self):
        pass

    def close(self):
        self.is_open = False


def respuesta(seq, error=0, campos=(), checksum_ok=True):
    datos = bytearray([seq, 0, 0, P.FLD, 0xC0, 0x80, P.FLD, P.FLD, error >> 8, error & 0xFF, P.FLD])
    for c in campos:
        datos.append(P.FLD)
        datos.extend(c.encode())
    trama = bytearray([P.STX])
    for b in datos:
        if b in (0x02, 0x03, 0x06, 0x1A, 0x1B, 0x1D, 0x1E, 0x1F):
            trama.append(P.ESC)
        trama.append(b)
    trama.append(P.ETX)
    suma = sum(trama) & 0xFFFF
    trama.extend(f"{suma if checksum_ok else suma ^ 1:04X}".encode())
    return bytes(trama)


def serie_con(respuestas, incremental=False):
    t = TransporteSerie("COM9", 115200, incremental)
    t._serie = PuertoFalso(respuestas)
    return t


def test_nak_reenvia_la_trama(tmp_path):
    registro.iniciar(str(tmp_path), False, True)
    t = serie_con([bytes([P.NAK]), respuesta(0xA9, campos=["800000"])])
    r = t.enviar(0x0B0B, 0, None, 2000)
    tramas = [w for w in t._serie.escrito if len(w) > 1]
    assert r.campo(0) == "800000" and len(tramas) == 2 and tramas[0] == tramas[1]
    assert t._serie.escrito[-1] == bytes([P.ACK])


def test_checksum_malo_contesta_nak(tmp_path):
    registro.iniciar(str(tmp_path), False, True)
    t = serie_con([respuesta(0xA9, checksum_ok=False) + respuesta(0xA9, campos=["ok"])])
    assert t.enviar(0x0001, 0, None, 2000).campo(0) == "ok"
    assert bytes([P.NAK]) in t._serie.escrito


def test_procesando_renueva_la_espera(tmp_path):
    registro.iniciar(str(tmp_path), False, True)
    procesando = bytes([P.STX, 0x80, P.ETX]) + f"{(P.STX + 0x80 + P.ETX):04X}".encode()
    t = serie_con([procesando + bytes([P.DC4, P.DC4]) + respuesta(0xA9, campos=["00091"])])
    assert t.enviar(0x0801, 0, None, 2000).campo(0) == "00091"


def test_secuencia_incremental_descarta_respuesta_vieja(tmp_path):
    registro.iniciar(str(tmp_path), False, True)
    t = serie_con([respuesta(0x99, campos=["vieja"]) + respuesta(0x81, campos=["nueva"])], incremental=True)
    assert t.enviar(0x0B07, 0, None, 2000).campo(0) == "nueva"
    assert t._seq == 0x82


def test_sin_respuesta_es_error_de_comunicacion(tmp_path):
    registro.iniciar(str(tmp_path), False, True)
    t = serie_con([])
    with pytest.raises(P.ErrorComunicacion, match="no respondio"):
        t.enviar(0x0001, 0, None, 300)
