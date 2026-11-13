"""Canal hacia el impresor: puerto serie real o simulador en memoria.

Portado de genesis-fiscal (Transporte.cs), con dos cambios en el puerto serie:
- ante NAK se reenvia la trama (hasta 3 veces);
- con secuencia incremental se descartan las respuestas cuya secuencia no es la del comando enviado.
"""
import threading
import time
from typing import List, Optional

from . import protocolo as P
from . import registro
from .protocolo import ErrorComunicacion, Respuesta

REENVIOS_NAK = 3


class Transporte:
    descripcion = ""

    def enviar(self, opcode: int, extension: int, campos: Optional[List[str]], timeout_ms: int) -> Respuesta:
        raise NotImplementedError

    def cerrar(self) -> None:
        pass


def detectar_puerto() -> Optional[str]:
    """Busca el adaptador USB CH340 del Genesis; el numero de COM cambia de caja en caja."""
    try:
        from serial.tools import list_ports
        for p in list_ports.comports():
            texto = " ".join(str(x or "") for x in (p.description, p.hwid, p.manufacturer))
            if "CH340" in texto.upper() or (p.vid == 0x1A86 and p.pid in (0x7523, 0x5523)):
                return p.device
    except Exception as ex:  # noqa: BLE001
        registro.log("No se pudo consultar los puertos: " + str(ex))
    return None


class TransporteSerie(Transporte):
    def __init__(self, puerto: str, baudios: int, secuencia_incremental: bool = False):
        self.puerto_configurado = puerto
        self.baudios = baudios
        self.incremental = secuencia_incremental
        self._seq = P.SECUENCIA_MIN
        self._serie = None
        self._nombre = ""

    @property
    def descripcion(self) -> str:
        return f"{self._nombre or self.puerto_configurado} a {self.baudios} baudios"

    def _siguiente_secuencia(self) -> int:
        if not self.incremental:
            return P.SECUENCIA_FIJA
        s = self._seq
        self._seq = P.SECUENCIA_MIN if s >= P.SECUENCIA_MAX else s + 1
        return s

    def _abrir(self) -> None:
        if self._serie is not None and self._serie.is_open:
            return
        self.cerrar()
        nombre = self.puerto_configurado
        if not nombre or nombre.lower() == "auto":
            nombre = detectar_puerto()
            if nombre is None:
                raise ErrorComunicacion(
                    "No se encuentra el USB del impresor fiscal (adaptador CH340). Revisar que este encendido y conectado.")
        try:
            import serial
            s = serial.Serial(port=nombre, baudrate=self.baudios, bytesize=8, parity="N", stopbits=1,
                              timeout=0.05, write_timeout=5, xonxoff=False, rtscts=False, dsrdtr=False)
            s.rts = True
            s.dtr = True
            time.sleep(0.2)
            self._serie, self._nombre = s, nombre
            registro.log(f"Puerto {nombre} abierto a {self.baudios} baudios")
        except Exception as ex:  # noqa: BLE001
            raise ErrorComunicacion(f"No se pudo abrir {nombre}: {ex} (otro programa puede tenerlo abierto)") from ex

    def cerrar(self) -> None:
        if self._serie is None:
            return
        try:
            self._serie.close()
        except Exception:  # noqa: BLE001
            pass
        self._serie = None

    def _leer_byte(self, limite: float) -> int:
        while time.monotonic() < limite:
            b = self._serie.read(1)
            if b:
                return b[0]
        return -1

    def _escribir(self, trama: bytes) -> None:
        # El impresor no acepta mas de unos 1200 bytes juntos.
        for i in range(0, len(trama), 1000):
            if i:
                time.sleep(0.2)
            self._serie.write(trama[i:i + 1000])
        self._serie.flush()

    def enviar(self, opcode, extension, campos, timeout_ms):
        nombre_cmd = f"{opcode:04X} ext {extension:04X}"
        try:
            self._abrir()
            seq = self._siguiente_secuencia()
            trama = P.armar(opcode, extension, campos, seq)
            registro.detalle(f"TX {nombre_cmd}: {P.hexa(trama)}")
            self._serie.reset_input_buffer()
            self._escribir(trama)
            paquete = self._leer_paquete(trama, seq, timeout_ms)
            if paquete is None:
                self.cerrar()
                raise ErrorComunicacion(f"El impresor fiscal no respondio al comando {nombre_cmd} en {timeout_ms // 1000} s")
            registro.detalle(f"RX {nombre_cmd}: {P.hexa(paquete)}")
            return P.interpretar(paquete)
        except ErrorComunicacion:
            raise
        except Exception as ex:  # noqa: BLE001
            self.cerrar()
            raise ErrorComunicacion(f"Fallo la comunicacion con el impresor fiscal ({nombre_cmd}): {ex}") from ex

    def _leer_paquete(self, trama: bytes, seq: int, timeout_ms: int) -> Optional[bytes]:
        limite = time.monotonic() + timeout_ms / 1000
        naks = 0
        while True:
            while True:
                b = self._leer_byte(limite)
                if b < 0:
                    return None
                if b == P.STX:
                    break
                if b == P.NAK:
                    naks += 1
                    if naks > REENVIOS_NAK:
                        return None
                    registro.log(f"El impresor pidio reenviar la trama (NAK {naks})")
                    self._escribir(trama)
                # ACK, DC2 y DC4 llegan fuera de trama (DC2 = poco papel).

            paquete = bytearray()
            suma = P.STX
            while True:
                b = self._leer_byte(limite)
                if b < 0:
                    return None
                suma += b
                if b == P.ESC:
                    b = self._leer_byte(limite)
                    if b < 0:
                        return None
                    suma += b
                    paquete.append(b)
                    continue
                if b == P.ETX:
                    break
                paquete.append(b)

            crc = bytearray()
            for _ in range(4):
                b = self._leer_byte(limite)
                if b < 0:
                    return None
                crc.append(b)
            if bytes(crc).decode("ascii", "replace") != f"{suma & 0xFFFF:04X}":
                self._serie.write(bytes([P.NAK]))
                continue
            if len(paquete) == 1 and paquete[0] == P.PROCESANDO:
                # "Procesando": el impresor sigue trabajando, se renueva la espera.
                limite = time.monotonic() + timeout_ms / 1000
                continue
            self._serie.write(bytes([P.ACK]))
            if self.incremental and paquete and paquete[0] != seq:
                registro.log(f"Respuesta con secuencia {paquete[0]:02X} (se esperaba {seq:02X}): se descarta")
                continue
            return bytes(paquete)


class TransporteSimulado(Transporte):
    """Impresor de mentira para probar el servicio sin emitir comprobantes.

    Mismo comportamiento que TransporteSimulado de genesis-fiscal: los numeradores, las respuestas
    y los errores coinciden, para poder comparar las dos implementaciones comando por comando.
    """

    descripcion = "SIMULADOR (no imprime ni emite nada)"

    def __init__(self):
        self.familia = 0
        self.total = 0
        self.pagado = 0
        self.items = 0
        self.numero_tique = 900001
        self.numero_factura = 900
        self.numero_zeta = 90
        self.numero_nc = 50
        self.letra = "B"
        self.historial: List[str] = []
        self._candado = threading.Lock()

    @staticmethod
    def _r(error=0, *campos) -> Respuesta:
        return Respuesta(estado_fiscal=0xC080, error=error, campos=list(campos))

    @staticmethod
    def _num(s: str) -> int:
        return int(s) if s and s.isdigit() and s.isascii() else 0

    def enviar(self, opcode, extension, campos, timeout_ms):
        with self._candado:
            return self._enviar(opcode, extension, list(campos or []))

    def _enviar(self, opcode, extension, campos):
        linea = f"{opcode:04X} ext {extension:04X} |" + "|".join(campos)
        self.historial.append(linea)
        registro.detalle("SIM " + linea)
        time.sleep(0.015)
        fam, cmd = opcode >> 8, opcode & 0xFF
        R = self._r

        if opcode == 0x0001:
            return R(0)
        if opcode in (0x0707, 0x0508, 0x050A):
            return R(0)
        if opcode == 0x0801:
            self.numero_zeta += 1
            return R(0, f"{self.numero_zeta:05d}")
        if opcode == 0x0803:
            return R(0, "00012")
        if opcode == 0x0E06:
            return R(0x0101)
        if fam not in (0x0A, 0x0B, 0x0D):
            return R(0x0202)

        if cmd == 0x01:
            if self.familia != 0:
                return R(0x0101)
            self.familia, self.total, self.pagado, self.items = fam, 0, 0, 0
            if fam != 0x0A:
                self.letra = "A" if len(campos) > 7 and campos[7] == "I" else "B"
            return R(0)
        if self.familia != fam:
            return R(0x0101)

        numero = f"{(self.numero_tique if fam == 0x0A else self.numero_factura if fam == 0x0B else self.numero_nc):08d}"
        if cmd == 0x02:
            if len(campos) < 15:
                return R(0x0204)
            tipo = extension & 0x0F
            importe = (self._num(campos[5]) * self._num(campos[6]) + 500000) // 1000000
            if importe <= 0:
                return R(0x0909)
            if tipo == 1:
                return R(0x0907)
            if tipo == 4:
                if self.total - importe <= 0:
                    return R(0x0909)
                self.total -= importe
            else:
                self.total += importe
            self.items += 1
            return R(0, str(self.total))
        if cmd == 0x05:
            if len(campos) < 7:
                return R(0x0204)
            self.pagado += self._num(campos[6])
            pendiente = max(0, self.total - self.pagado)
            vuelto = max(0, self.pagado - self.total)
            return R(0, "000" if pendiente == 0 else str(pendiente), "000" if vuelto == 0 else str(vuelto))
        if cmd == 0x06:
            if self.items == 0 or self.total <= 0:
                return R(0x0A05)
            iva = (self.total * 21 + 60) // 121
            vuelto = max(0, self.pagado - self.total)
            self.familia = 0
            v = "" if vuelto == 0 else str(vuelto)
            if fam == 0x0A:
                self.numero_tique += 1
                return R(0, numero, str(self.total), str(iva), v)
            if fam == 0x0B:
                self.numero_factura += 1
            else:
                self.numero_nc += 1
            return R(0, numero, self.letra, str(self.total), str(iva), v)
        if cmd == 0x07:
            self.familia = 0
            return R(0, numero) if fam == 0x0A else R(0, numero, self.letra)
        if cmd == 0x0A:
            if fam == 0x0A:
                return R(0, numero, str(self.total), str(self.pagado))
            return R(0, numero, self.letra, str(self.total), str(self.pagado))
        if cmd == 0x0B:
            iva = (self.total * 21 + 60) // 121
            return R(0, str(iva), "2100", str(iva))
        if cmd == 0x0E:
            return R(0, "000", "000", "000", "2100")
        return R(0x0202)
