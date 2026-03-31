import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import serial
from flask import Flask, jsonify, request
from flask_cors import CORS


STX = 0x02
ETX = 0x03
SEP = 0x1C
ACK = 0x06
NAK = 0x15
WAIT_BYTES = {0x12, 0x14}

PKT_MIN = 0x80
PKT_MAX = 0xFF

ERROR_MESSAGES = {
    "0000": "Sin error",
}

FAMILY_TIQUE = "tique"
FAMILY_TIQUE_FACTURA = "tique_factura"
FAMILY_NOTA_CREDITO = "nota_credito"
FAMILY_DNFH = "dnfh"

# Conjuntos base para resolver familia a partir del campo type.
TYPE_FAMILY_MAP = {
    83: FAMILY_TIQUE_FACTURA,
    91: FAMILY_DNFH,
}

# Tipos comunes usados para identificar notas de credito/debito en flujo 0B/0D.
NOTA_CREDITO_TYPES = {3, 8, 13, 53, 58}
NOTA_DEBITO_TYPES = {2, 7, 12, 52, 57}


class FiscalPrinterError(Exception):
    def __init__(self, message: str, detail: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.detail = detail or {}


@dataclass
class FiscalResponse:
    raw_payload: bytes
    hardware_status: str
    fiscal_status: str
    error_code: str
    fields: List[str]
    response_fields: List[str]


def calculate_crc(packet_from_stx_to_etx: bytes) -> str:
    """
    Suma todos los bytes desde STX hasta ETX inclusive y devuelve
    los ultimos 4 digitos hexadecimales en mayusculas.
    """
    checksum = sum(packet_from_stx_to_etx) & 0xFFFF
    return f"{checksum:04X}"


def split_command(command_4_hex: str) -> Tuple[str, str]:
    cmd = (command_4_hex or "").strip().upper()
    if len(cmd) != 4 or any(ch not in "0123456789ABCDEF" for ch in cmd):
        raise ValueError(f"Comando invalido: {command_4_hex}")
    return cmd[:2], cmd[2:]


def _field_to_status_hex(field: bytes) -> str:
    if not field:
        return "0000"

    # Si viene como texto hexadecimal ASCII (ej. b'00AF'), usarlo tal cual.
    if all((48 <= b <= 57) or (65 <= b <= 70) or (97 <= b <= 102) for b in field):
        text = field.decode("ascii").upper()
        return text.zfill(4)[-4:]

    # Si viene binario, convertir a hex y conservar ultimos 4 digitos.
    return field.hex().upper().zfill(4)[-4:]


class GenesisFiscalPrinter:
    def __init__(
        self,
        port: str,
        baudrate: int = 115200,
        timeout: float = 1.0,
        read_timeout: float = 120.0,
    ):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.read_timeout = read_timeout
        self._pkt_num = PKT_MIN
        self._serial: Optional[serial.Serial] = None
        self._lock = threading.Lock()

    def _ensure_open(self) -> None:
        if self._serial and self._serial.is_open:
            return

        self._serial = serial.Serial(
            port=self.port,
            baudrate=self.baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=self.timeout,
            write_timeout=self.timeout,
        )

    def _next_pkt_num(self) -> int:
        pkt = self._pkt_num
        self._pkt_num = PKT_MIN if self._pkt_num >= PKT_MAX else self._pkt_num + 1
        return pkt

    def _build_packet(self, command: str, extension: str, args: List[str]) -> bytes:
        pkt_num = self._next_pkt_num()

        body = bytearray()
        body.append(STX)
        body.append(pkt_num)
        body.extend(command.encode("ascii"))
        body.append(SEP)
        body.extend(extension.encode("ascii"))
        body.append(SEP)

        for idx, arg in enumerate(args):
            if idx > 0:
                body.append(SEP)
            body.extend(str(arg).encode("latin-1", errors="replace"))

        body.append(ETX)
        crc = calculate_crc(bytes(body)).encode("ascii")
        return bytes(body) + crc

    def _read_response_packet(self) -> FiscalResponse:
        if not self._serial:
            raise FiscalPrinterError("Puerto serial no inicializado")

        start = time.monotonic()

        while True:
            if time.monotonic() - start > self.read_timeout:
                raise FiscalPrinterError("Timeout esperando inicio de respuesta fiscal")

            raw = self._serial.read(1)
            if not raw:
                continue

            byte = raw[0]

            if byte in WAIT_BYTES:
                # WAIT1 (0x12) o WAIT2 (0x14): la impresora sigue ocupada.
                continue

            if byte == ACK:
                # ACK previo al paquete de datos.
                continue

            if byte == NAK:
                raise FiscalPrinterError("NAK recibido desde impresora")

            if byte != STX:
                # Ignorar ruido y seguir escuchando hasta STX.
                continue

            packet = bytearray([STX])
            while True:
                if time.monotonic() - start > self.read_timeout:
                    raise FiscalPrinterError("Timeout recibiendo paquete fiscal")

                ch = self._serial.read(1)
                if not ch:
                    continue

                packet.append(ch[0])
                if ch[0] == ETX:
                    break

            crc = self._serial.read(4)
            if len(crc) != 4:
                raise FiscalPrinterError("CRC incompleto en respuesta fiscal")

            expected_crc = calculate_crc(bytes(packet)).encode("ascii")
            if crc.upper() != expected_crc:
                raise FiscalPrinterError(
                    "CRC de respuesta invalido",
                    detail={
                        "crc_recibido": crc.decode("ascii", errors="replace"),
                        "crc_esperado": expected_crc.decode("ascii"),
                    },
                )

            payload = bytes(packet[1:-1])
            if payload and PKT_MIN <= payload[0] <= PKT_MAX:
                payload = payload[1:]

            parts = payload.split(bytes([SEP])) if payload else []
            hardware_status = _field_to_status_hex(parts[0]) if len(parts) >= 1 else "0000"
            fiscal_status = _field_to_status_hex(parts[1]) if len(parts) >= 2 else "0000"
            error_code = _field_to_status_hex(parts[2]) if len(parts) >= 3 else "0000"

            decoded_fields = [p.decode("latin-1", errors="replace") for p in parts]
            response_fields = decoded_fields[3:] if len(decoded_fields) > 3 else []

            return FiscalResponse(
                raw_payload=payload,
                hardware_status=hardware_status,
                fiscal_status=fiscal_status,
                error_code=error_code,
                fields=decoded_fields,
                response_fields=response_fields,
            )

    def send_command(
        self,
        command_4_hex: str,
        args: Optional[List[str]] = None,
        extension_override: Optional[str] = None,
    ) -> FiscalResponse:
        args = args or []
        command, extension = split_command(command_4_hex)
        if extension_override is not None:
            extension = extension_override.upper()

        packet = self._build_packet(command, extension, args)

        with self._lock:
            self._ensure_open()
            assert self._serial is not None
            self._serial.reset_input_buffer()
            self._serial.write(packet)
            self._serial.flush()
            response = self._read_response_packet()

        return response


app = Flask(__name__)
CORS(app)

printer = GenesisFiscalPrinter(
    port=os.getenv("FISCAL_SERIAL_PORT", "/dev/ttyUSB0"),
    baudrate=int(os.getenv("FISCAL_BAUDRATE", "115200")),
    timeout=float(os.getenv("FISCAL_PORT_TIMEOUT", "1.0")),
    read_timeout=float(os.getenv("FISCAL_READ_TIMEOUT", "120.0")),
)


def response_to_dict(resp: FiscalResponse) -> Dict[str, Any]:
    return {
        "estado_hardware": resp.hardware_status,
        "estado_fiscal": resp.fiscal_status,
        "cod_error": resp.error_code,
        "descripcion_error": ERROR_MESSAGES.get(resp.error_code, "Error fiscal no mapeado"),
        "campos": resp.fields,
    }


def ensure_ok(resp: FiscalResponse, command: str) -> None:
    if resp.error_code != "0000":
        raise FiscalPrinterError(
            f"Error fiscal al ejecutar {command}",
            detail={
                "command": command,
                **response_to_dict(resp),
            },
        )


def to_n_format(value: Any, decimals: int) -> str:
    numeric = float(value)
    return f"{numeric:.{decimals}f}"


def iva_to_protocol(iva_value: Any) -> str:
    iva = float(iva_value)
    return str(int(round(iva * 100)))


def resolve_doc_family(doc_type: int) -> str:
    if doc_type in TYPE_FAMILY_MAP:
        return TYPE_FAMILY_MAP[doc_type]
    if doc_type in NOTA_CREDITO_TYPES:
        return FAMILY_NOTA_CREDITO
    # Si el tipo esta en rango alto no fiscal o marcado como remito/documento no fiscal.
    if doc_type >= 90:
        return FAMILY_DNFH
    return FAMILY_TIQUE


def resolve_commands(doc_family: str) -> Dict[str, str]:
    if doc_family == FAMILY_TIQUE:
        return {"open": "0A01", "item": "0A02", "pay": "0A05", "close": "0A06"}
    if doc_family == FAMILY_TIQUE_FACTURA:
        return {"open": "0B01", "item": "0B02", "pay": "0B05", "close": "0B06"}
    if doc_family == FAMILY_NOTA_CREDITO:
        return {"open": "0D01", "item": "0D02", "pay": "0D05", "close": "0D06"}
    if doc_family == FAMILY_DNFH:
        return {"open": "1001", "item": "1002", "pay": "1005", "close": "1006"}
    raise ValueError(f"Familia de comprobante no soportada: {doc_family}")


def build_extension(base_command: str, enabled_bits: int = 0, disabled_bits: int = 0) -> str:
    _, extension = split_command(base_command)
    ext_value = int(extension, 16)
    ext_value |= enabled_bits
    ext_value &= ~disabled_bits
    return f"{ext_value:04X}"


def parse_amount(value: str, decimals: int = 2) -> float:
    raw = (value or "0").strip().replace(",", ".")
    if not raw:
        return 0.0

    if "." in raw:
        return float(raw)

    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        return 0.0

    return int(digits) / (10 ** decimals)


def parse_close_fiscal_data(resp: FiscalResponse, doc_family: str) -> Dict[str, Any]:
    response = resp.response_fields

    numero_ticket = response[0] if len(response) > 0 else ""
    total_bruto = parse_amount(response[1]) if len(response) > 1 else 0.0
    total_iva = parse_amount(response[2]) if len(response) > 2 else 0.0
    vuelto = parse_amount(response[3]) if len(response) > 3 else 0.0

    tipo_comprobante = None
    if doc_family in {FAMILY_TIQUE_FACTURA, FAMILY_NOTA_CREDITO, FAMILY_DNFH} and len(response) > 4:
        tipo_comprobante = response[4]

    return {
        "numero_ticket": numero_ticket,
        "tipo_comprobante": tipo_comprobante,
        "total_bruto": total_bruto,
        "total_iva": total_iva,
        "vuelto": vuelto,
    }


@app.errorhandler(FiscalPrinterError)
def handle_fiscal_error(exc: FiscalPrinterError):
    payload = {"ok": False, "error": str(exc), "detail": exc.detail}
    return jsonify(payload), 400


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "service": "moretti-genesis-flask"})


@app.route("/imprimir_ticket", methods=["POST"])
def imprimir_ticket():
    data = request.get_json(silent=True) or {}
    doc_type = int(data.get("type", 83))
    doc_family = resolve_doc_family(doc_type)
    commands = resolve_commands(doc_family)
    is_nota_debito = bool(data.get("es_nota_debito", False)) or doc_type in NOTA_DEBITO_TYPES
    cliente = data.get("cliente", {})
    items = data.get("items", [])
    pagos = data.get("pagos", [])
    usar_cutter = bool(data.get("usar_cutter", True))

    if not isinstance(items, list) or not items:
        return jsonify({"ok": False, "error": "Se requiere una lista no vacia en 'items'"}), 400

    if not isinstance(pagos, list) or not pagos:
        return jsonify({"ok": False, "error": "Se requiere una lista no vacia en 'pagos'"}), 400

    status_resp = printer.send_command("0001", [])
    ensure_ok(status_resp, "0001")

    open_args: List[str] = []
    open_extension: Optional[str] = None

    try:
        if doc_family == FAMILY_TIQUE_FACTURA:
            open_args = [
                str(cliente.get("nombre_o_razon_social1", "CONSUMIDOR FINAL")),
                str(cliente.get("domicilio1", "")),
                str(cliente.get("id_tipo_documento", "D")),
                str(cliente.get("numero_documento", "0")),
                str(cliente.get("id_responsabilidad_iva", "F")),
            ]
            if is_nota_debito:
                # Bit 13 activo para Nota de Debito en apertura 0B01.
                open_extension = build_extension(commands["open"], enabled_bits=(1 << 13))
        elif doc_family == FAMILY_DNFH:
            # En 1001 se envia Tipo Doc DNFH (ej: 91 remito R) en argumentos.
            open_args = [str(doc_type)]
        else:
            open_args = []
    except (TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": f"Datos de apertura invalidos: {exc}"}), 400

    open_resp = printer.send_command(commands["open"], open_args, extension_override=open_extension)
    ensure_ok(open_resp, commands["open"])

    total = 0.0
    item_responses = []

    for idx, item in enumerate(items, start=1):
        try:
            descripcion = str(item.get("description", item.get("descripcion", "")))
            if not descripcion:
                raise KeyError("description")
            cantidad = float(item.get("qty", item.get("cantidad")))
            precio_unitario = float(item.get("price", item.get("precio_unitario")))
            iva_valor = float(item.get("iva", 21))
            codigo_interno = str(item.get("code_intern", ""))
        except (KeyError, TypeError, ValueError) as exc:
            return jsonify({"ok": False, "error": f"Item {idx} invalido: {exc}"}), 400

        total += cantidad * precio_unitario
        condicion_iva = "7" if iva_valor > 0 else "2"

        args = [
            descripcion,
            to_n_format(cantidad, 4),
            to_n_format(precio_unitario, 4),
            iva_to_protocol(iva_valor),
            condicion_iva,
            codigo_interno,
        ]

        item_resp = printer.send_command(commands["item"], args)
        ensure_ok(item_resp, commands["item"])
        item_responses.append(response_to_dict(item_resp))

    pago_responses = []
    total_pagado = 0.0

    for idx, pago in enumerate(pagos, start=1):
        try:
            codigo_pago = str(pago.get("codigo_forma_pago", pago.get("codigo")))
            if not codigo_pago or codigo_pago == "None":
                raise KeyError("codigo_forma_pago")
            monto_pago = float(pago["monto"])
            descripcion_pago = str(pago.get("descripcion", "PAGO"))
        except (KeyError, TypeError, ValueError) as exc:
            return jsonify({"ok": False, "error": f"Pago {idx} invalido: {exc}"}), 400

        total_pagado += monto_pago

        pay_resp = printer.send_command(
            commands["pay"],
            [codigo_pago, to_n_format(monto_pago, 2), descripcion_pago],
        )
        ensure_ok(pay_resp, commands["pay"])
        pago_responses.append(response_to_dict(pay_resp))

    close_extension = None
    if usar_cutter:
        close_extension = build_extension(commands["close"], enabled_bits=1)
    else:
        close_extension = build_extension(commands["close"], disabled_bits=1)

    close_resp = printer.send_command(commands["close"], [], extension_override=close_extension)
    ensure_ok(close_resp, commands["close"])
    fiscal_data = parse_close_fiscal_data(close_resp, doc_family)

    return jsonify(
        {
            "status": "success",
            "fiscal_data": fiscal_data,
            "ok": True,
            "tipo": doc_type,
            "familia": doc_family,
            "estado_inicial": response_to_dict(status_resp),
            "apertura": response_to_dict(open_resp),
            "items": item_responses,
            "pagos": pago_responses,
            "cierre": response_to_dict(close_resp),
            "total": round(total, 2),
            "total_pagado": round(total_pagado, 2),
        }
    )


@app.route("/cierre_z", methods=["POST"])
def cierre_z():
    resp = printer.send_command("0801", [])
    ensure_ok(resp, "0801")

    numero_zeta = resp.fields[3] if len(resp.fields) > 3 else None

    return jsonify(
        {
            "ok": True,
            "cierre_z": response_to_dict(resp),
            "numero_zeta": numero_zeta,
        }
    )


if __name__ == "__main__":
    host = os.getenv("FLASK_HOST", "0.0.0.0")
    port = int(os.getenv("FLASK_PORT", "5000"))
    debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    app.run(host=host, port=port, debug=debug)
