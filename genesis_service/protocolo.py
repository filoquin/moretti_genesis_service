"""Trama del protocolo Moretti Genesis: armado, lectura, texto y codigos de error.

Ver docs/protocolo.md. Portado de genesis-fiscal (servicio/src/Protocolo.cs).
"""
import unicodedata
from dataclasses import dataclass, field
from typing import List, Optional

STX, ETX, ACK, NAK, ESC, FLD = 0x02, 0x03, 0x06, 0x15, 0x1B, 0x1C
DC2, DC4 = 0x12, 0x14
PROCESANDO = 0x80

# Secuencia fija, como ProtoEmul (lo verificado en el impresor). Con secuencia incremental se usa
# el rango 0x81..0xFE: 0x80 es el aviso de "procesando".
SECUENCIA_FIJA = 0xA9
SECUENCIA_MIN, SECUENCIA_MAX = 0x81, 0xFE

# ProtoEmul escapa tambien 06 (la libreria cf.net no).
_ESCAPADOS = {0x02, 0x03, 0x06, 0x1A, 0x1B, 0x1D, 0x1E, 0x1F}


class ErrorPedido(Exception):
    """El pedido no se puede emitir (validacion, duplicado, datos faltantes)."""


class ErrorDuplicado(ErrorPedido):
    """El pedido ya se emitio (o quedo incierto): no se emite de nuevo sin forzar."""


class ErrorImpresor(Exception):
    """El impresor contesto con un codigo de error."""

    def __init__(self, mensaje: str, codigo: int):
        super().__init__(mensaje)
        self.codigo = codigo


class ErrorComunicacion(Exception):
    """No hubo respuesta, o fallo el puerto, o la respuesta no tiene el formato esperado."""


@dataclass
class Respuesta:
    estado_impresora: int = 0
    estado_fiscal: int = 0
    error: int = 0
    campos: List[str] = field(default_factory=list)
    secuencia: Optional[int] = None

    @property
    def ok(self) -> bool:
        return self.error == 0

    def campo(self, i: int) -> str:
        return self.campos[i] if 0 <= i < len(self.campos) else ""

    @property
    def error_texto(self) -> str:
        return f"{self.error:04X} {describir_error(self.error)}"


def armar(opcode: int, extension: int, campos: Optional[List[str]], secuencia: int = SECUENCIA_FIJA) -> bytes:
    datos = bytearray([secuencia, (opcode >> 8) & 0xFF, opcode & 0xFF, FLD, (extension >> 8) & 0xFF, extension & 0xFF])
    for c in campos or []:
        datos.append(FLD)
        datos.extend(limpiar(c).encode("latin-1"))

    trama = bytearray([STX])
    for b in datos:
        if b in _ESCAPADOS:
            trama.append(ESC)
        trama.append(b)
    trama.append(ETX)
    trama.extend(f"{sum(trama) & 0xFFFF:04X}".encode("ascii"))
    return bytes(trama)


def interpretar(paquete: bytes) -> Respuesta:
    """paquete = datos entre STX y ETX, ya sin escapes.

    seq | estado impresora (2) | 1C | estado fiscal (2) | 1C | 1C | error (2) | 1C | [1C campo]...
    """
    if paquete is None or len(paquete) < 11:
        raise ErrorComunicacion(f"Respuesta del impresor demasiado corta ({0 if paquete is None else len(paquete)} bytes)")
    if paquete[3] != FLD or paquete[6] != FLD or paquete[7] != FLD or paquete[10] != FLD:
        raise ErrorComunicacion("Respuesta del impresor con formato inesperado: " + hexa(paquete))
    r = Respuesta(
        estado_impresora=paquete[1] * 0x100 + paquete[2],
        estado_fiscal=paquete[4] * 0x100 + paquete[5],
        error=paquete[8] * 0x100 + paquete[9],
        secuencia=paquete[0],
    )
    i = 11
    while i < len(paquete):
        if paquete[i] != FLD:
            break
        i += 1
        inicio = i
        while i < len(paquete) and paquete[i] != FLD:
            i += 1
        r.campos.append(paquete[inicio:i].decode("latin-1"))
    return r


def hexa(datos: bytes) -> str:
    return " ".join(f"{b:02X}" for b in datos)


# --- Texto ---------------------------------------------------------------------------------

def limpiar(s) -> str:
    """El impresor solo recibe ASCII imprimible: se quitan acentos y lo demas se descarta."""
    if not s:
        return ""
    out = []
    for ch in unicodedata.normalize("NFD", str(s)):
        if unicodedata.category(ch) == "Mn":
            continue
        if ch in "º°":
            out.append("o")
        elif ch == "ª":
            out.append("a")
        elif " " <= ch < "\x7f":
            out.append(ch)
        elif ch in "\t\r\n":
            out.append(" ")
    return "".join(out)


def recortar(s, maximo: int) -> str:
    s = limpiar(s).strip()
    return s[:maximo].strip() if len(s) > maximo else s


def partir(texto, ancho_extra: int, ancho_final: int, max_lineas: int) -> List[str]:
    """Parte un nombre largo en lineas: las primeras de hasta ancho_extra y la ultima
    (la descripcion principal del item) de hasta ancho_final."""
    lineas: List[str] = []
    resto = limpiar(texto).strip()
    while resto:
        if len(resto) <= ancho_final:
            lineas.append(resto)
            break
        corte = min(ancho_extra, len(resto))
        if corte < len(resto):
            esp = resto.rfind(" ", 0, corte + 1)
            if esp > 0:
                corte = esp
        else:
            # Entra en una linea extra pero no en la descripcion: dejar la ultima palabra para el final.
            esp = resto.rfind(" ")
            if esp > 0 and len(resto) - esp - 1 <= ancho_final:
                corte = esp
            else:
                corte = max(1, len(resto) - ancho_final)
        lineas.append(resto[:corte].strip())
        resto = resto[corte:].strip()
    if not lineas:
        lineas.append("")
    if len(lineas) > max_lineas:
        lineas = lineas[:max_lineas]
        ultima = lineas[-1]
        if len(ultima) > ancho_final:
            lineas[-1] = ultima[:ancho_final].strip()
    return lineas


# --- Codigos de error ----------------------------------------------------------------------

_ERRORES = {
    0x0001: "Error interno del impresor",
    0x0005: "Comando no implementado",
    0x0007: "El monto maximo por item es 9999999.99",
    0x0101: "Comando invalido para el estado actual",
    0x0102: "Comando invalido para el documento actual",
    0x0103: "Comando solo aceptado en modo tecnico",
    0x0201: "La trama no tiene el largo minimo",
    0x0202: "Comando invalido",
    0x0203: "El comando tiene mas campos de los que soporta",
    0x0204: "El comando tiene menos campos de los que necesita",
    0x0205: "Falta un campo obligatorio",
    0x0301: "Error de hardware",
    0x0302: "Impresora fuera de linea",
    0x0303: "Error de impresion",
    0x0304: "Falta papel en el impresor",
    0x0801: "Requiere periodo de actividades iniciado",
    0x0902: "Underflow",
    0x0903: "Demasiados items en la operacion",
    0x0904: "Demasiadas tasas de impuesto utilizadas",
    0x0905: "Demasiados descuentos o ajustes en la operacion",
    0x0906: "Demasiados pagos en la operacion",
    0x0907: "Item no encontrado o ya anulado",
    0x0908: "Pago no encontrado",
    0x0909: "El total debe ser mayor a cero",
    0x090A: "Se permite un solo tipo de impuestos internos",
    0x0A03: "Tipo de item invalido",
    0x0A04: "Linea de descripcion en blanco",
    0x0A05: "Cantidad resultante menor que cero",
    0x0A0B: "Monto de pago no permitido",
    0x0A10: "Demasiadas descripciones extras",
    0x0A11: "Falta numero de cupon en tarjeta de credito/debito",
    0x0A12: "Falta la marca de la tarjeta de credito/debito",
    0x0B01: "Tipo de documento del comprador invalido",
    0x0B03: "CUIT / CUIL invalido",
    0x0B0D: "Numero de documento invalido",
    0x0B11: "Tipo de documento asociado invalido",
    0x0B12: "Punto de venta de documento asociado invalido",
    0x0B13: "Numero de documento asociado invalido",
}


def describir_error(codigo: int) -> str:
    return _ERRORES.get(codigo, "Error del impresor (ver Codigos_de_Error de Moretti)")
