"""Operaciones sobre el impresor: emitir comprobante, cierres y estado.

Portado de genesis-fiscal (Genesis.cs). La secuencia de comandos copia la que envia ProtoEmul en
produccion y es la que se verifico en el impresor real el 12/11/2026: no cambiarla sin volver a probar.

Unica diferencia de comportamiento con genesis-fiscal: el numero de pedido que va en la primera
linea extra del primer item se recorta a ANCHO_EXTRA (en genesis-fiscal un nombre largo hacia que el
impresor rechazara el item).
"""
import threading
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import List, Optional

from . import registro
from .comprobante import CERO, Cliente, Comprobante, Item, redondear
from .protocolo import ErrorComunicacion, ErrorDuplicado, ErrorImpresor, ErrorPedido, Respuesta, partir, recortar
from .registro import Emitido
from .transporte import Transporte

T_NORMAL, T_CIERRE, T_ZETA = 5000, 60000, 180000
ESPERA_PAPEL_S = 60
ANCHO_EXTRA, ANCHO_DESCRIPCION, ANCHO_PAPEL = 28, 20, 48
NADA: List[str] = []


@dataclass
class ResultadoEmision:
    numero: str = ""
    letra: str = ""
    total: Decimal = CERO
    iva: Decimal = CERO
    vuelto: Decimal = CERO
    tipo: str = ""


def fijo(valor: Decimal, decimales: int) -> str:
    return str(int(redondear(valor * (Decimal(10) ** decimales), 0)))


def centavos(campo: str) -> Decimal:
    return Decimal(int(campo)) / 100 if campo and campo.isdigit() and campo.isascii() else CERO


def pesos(valor: Decimal) -> str:
    return str(redondear(valor, 2)).replace(".", ",")


def _codigo_pago_valido(c: int) -> bool:
    return 1 <= c <= 24 or c == 99


class Genesis:
    def __init__(self, transporte: Transporte, abrir_cajon: bool):
        self.transporte = transporte
        self.abrir_cajon = abrir_cajon
        self._candado = threading.Lock()

    @property
    def canal(self) -> str:
        return self.transporte.descripcion

    # --- comandos ----------------------------------------------------------------------------

    def _cmd(self, opcode: int, ext: int, campos, timeout_ms: int) -> Respuesta:
        """Envia un comando. Si falta papel espera a que lo repongan (hasta un minuto)."""
        limite = time.monotonic() + ESPERA_PAPEL_S
        while True:
            r = self.transporte.enviar(opcode, ext, campos, timeout_ms)
            if r.error != 0x0304 or time.monotonic() > limite:
                return r
            registro.log("Falta papel en el impresor; esperando...")
            time.sleep(3)

    def _exigir(self, que: str, opcode: int, ext: int, campos, timeout_ms: int) -> Respuesta:
        r = self._cmd(opcode, ext, campos, timeout_ms)
        if not r.ok:
            raise ErrorImpresor(f"El impresor fiscal rechazo {que}: {r.error_texto}", r.error)
        return r

    def _tolerar(self, que: str, opcode: int, ext: int, campos) -> None:
        r = self._cmd(opcode, ext, campos, T_NORMAL)
        if not r.ok:
            registro.log(f"Aviso: {que} devolvio {r.error_texto}")

    def _anular_pendientes(self) -> None:
        """Anula cualquier documento que haya quedado abierto de un intento anterior."""
        for op in (0x0B07, 0x0A07, 0x0D07):
            r = self._cmd(op, 0, NADA, T_CIERRE)
            if r.ok:
                registro.log(f"ATENCION: habia un documento abierto y se anulo ({op:04X} -> {'|'.join(r.campos)})")
            elif r.error != 0x0101:
                registro.log(f"Aviso: anulacion {op:04X} devolvio {r.error_texto}")
        nf = self._cmd(0x0E06, 0, [""] * 6, T_CIERRE)
        if nf.ok:
            registro.log("ATENCION: habia un documento no fiscal abierto y se cerro")

    # --- campos ------------------------------------------------------------------------------

    @staticmethod
    def _campos_cliente(c: Cliente) -> List[str]:
        tipo_doc, resp = c.tipo_documento, c.responsabilidad
        if len(tipo_doc) != 1 or tipo_doc not in "DLTCPVE":
            tipo_doc = "D"
        if len(resp) != 1 or resp not in "INMEUFTP":
            resp = "F"
        R = recortar
        if not c.documento:
            # Igual que ProtoEmul: sin documento solo puede ser consumidor final anonimo.
            return ["(Sin nombre ni Razon Social)", "", "(Sin direccion)", "", "", "D", "", "F",
                    R(c.asociado1, 40), R(c.asociado2, 40), R(c.asociado3, 40), ""]
        nombre = c.nombre1 or "(Sin nombre ni Razon Social)"
        domicilio = c.domicilio1 or "(Sin direccion)"
        return [R(nombre, 40), R(c.nombre2, 40), R(domicilio, 40), R(c.domicilio2, 40), R(c.domicilio3, 40),
                tipo_doc, c.documento, resp,
                R(c.asociado1, 40), R(c.asociado2, 40), R(c.asociado3, 40), R(c.cheque, 40)]

    @staticmethod
    def _campos_item(it: Item, primera_linea: str, bonificacion: bool) -> List[str]:
        extras: List[str] = []
        if primera_linea:
            extras.append(primera_linea)
        lugares = 4 - len(extras) - (1 if bonificacion else 0)
        partes = partir(it.descripcion, ANCHO_EXTRA, ANCHO_DESCRIPCION, lugares + 1)
        descripcion = partes[-1] or "Item"
        extras.extend(partes[:-1])
        cantidad, precio = abs(it.cantidad), abs(it.precio)
        if bonificacion:
            while len(extras) < 2:
                extras.append("")
            extras.insert(2, str(redondear(cantidad, 3)).replace(".", ",") + " x " + pesos(precio))
        while len(extras) < 4:
            extras.append("")
        return [extras[0], extras[1], extras[2], extras[3], descripcion,
                fijo(cantidad, 4), fijo(precio, 4), fijo(it.iva, 2),
                "", "", "1", "", "", "7", "7"]

    # --- emitir ------------------------------------------------------------------------------

    def emitir(self, c: Comprobante, forzar: bool = False) -> ResultadoEmision:
        with self._candado:
            c.validar()

            previo = registro.buscar(c.nombre)
            if previo is not None and not forzar:
                if previo.Estado == "incierto":
                    raise ErrorDuplicado(
                        f"El pedido {c.nombre} ya se intento emitir el {previo.Fecha} y no se pudo confirmar si salio "
                        f"(comprobante {previo.Numero}). Revisar el papel antes de reintentar.")
                tipo = previo.Tipo or "comprobante"
                raise ErrorDuplicado(
                    f"El pedido {c.nombre} ya se emitio el {previo.Fecha} como {tipo} "
                    f"{(previo.Letra + ' ' + previo.Numero).strip()} por ${previo.Total}. No se emite de nuevo.")

            # Familias: 0A tique (o nota de credito a consumidor final), 0B tique factura, 0D nota de credito A/B.
            es_nc = c.es_nota_credito
            con_cliente = (len(c.cliente.documento) > 0) if c.tipo == 110 else not c.es_tique
            if es_nc and con_cliente and not c.cliente.asociado1:
                # La nota de credito A/B exige el comprobante original (el impresor da 0B11 si falta).
                # Sin ese dato, a un consumidor final anonimo se le emite la nota de credito a consumidor final.
                if c.cliente.documento:
                    raise ErrorPedido(
                        "La nota de credito a un cliente identificado necesita el comprobante original en "
                        "documento_asociado1 (tipo-punto de venta-numero, por ejemplo 082-00010-00000359). "
                        "No se emitio nada.")
                con_cliente = False
            fam = 0x0A if not con_cliente else (0x0D if es_nc else 0x0B)
            op = fam << 8
            signo = 1 if (c.tipo == 110 and c.total > 0) else (-1 if es_nc else 1)
            total_pedido = abs(c.total)
            if es_nc:
                tipo_texto = "nota de credito a consumidor final" if fam == 0x0A else "nota de credito"
            else:
                tipo_texto = "tique" if fam == 0x0A else "tique factura"
            registro.log(f"Pedido '{c.nombre}': {tipo_texto} (tipo {c.tipo}), {len(c.items)} items, "
                         f"total calculado {pesos(total_pedido)}")
            if c.descuento_informado != 0:
                registro.log(f"  (se ignora 'descuentos' = {pesos(c.descuento_informado)}: "
                             "el precio de cada item ya trae el descuento)")

            self._anular_pendientes()

            abierto = False
            numero = ""
            try:
                if fam == 0x0B:
                    self._tolerar("limpiar encabezado 9", 0x0508, 0, ["9", ""])
                    self._tolerar("limpiar encabezado 10", 0x0508, 0, ["10", ""])
                    self._exigir("la apertura del tique factura", 0x0B01, 0, self._campos_cliente(c.cliente), T_NORMAL)
                elif fam == 0x0D:
                    self._exigir("la apertura de la nota de credito", 0x0D01, 0, self._campos_cliente(c.cliente), T_NORMAL)
                else:
                    self._exigir("la apertura del " + ("tique nota de credito" if es_nc else "tique"),
                                 0x0A01, 0x4000 if es_nc else 0, NADA, T_NORMAL)
                abierto = True

                info = self._cmd(op | 0x0A, 0, NADA, T_NORMAL)
                if info.ok:
                    numero = info.campo(0)

                if self.abrir_cajon and not es_nc:
                    self._tolerar("abrir el cajon", 0x0707, 0, NADA)

                # Primero las ventas y despues las devoluciones (como bonificacion), para que el total nunca
                # quede en cero. En una nota de credito es al reves: primero lo devuelto y despues lo que resta.
                ext_base = (0 if fam == 0x0A else 0x4000) | 0x0050
                primera = recortar(c.nombre, ANCHO_EXTRA)
                negativos: List[Item] = []
                for it in c.items:
                    importe = signo * redondear(it.importe, 2)
                    if importe == 0:
                        registro.log("  item sin importe, no se informa: " + it.descripcion)
                        continue
                    if importe < 0:
                        negativos.append(it)
                        continue
                    self._exigir(f"el item '{it.descripcion}'", op | 0x02, ext_base,
                                 self._campos_item(it, primera, False), T_NORMAL)
                    primera = ""
                for it in negativos:
                    self._exigir(f"la linea de signo contrario '{it.descripcion}'", op | 0x02, ext_base | 0x04,
                                 self._campos_item(it, "", True), T_NORMAL)

                for p in c.pagos:
                    # En un reembolso Odoo manda los pagos en negativo.
                    monto = abs(p.monto) if es_nc else p.monto
                    if monto <= 0:
                        continue
                    codigo = p.codigo if _codigo_pago_valido(p.codigo) else 99
                    # Tarjeta de credito y debito exigen numero de cupon y marca (0A11 y 0A12), que Odoo no manda.
                    if codigo in (20, 21):
                        codigo = 99
                    desc = "" if codigo == 8 else recortar(p.descripcion, ANCHO_EXTRA)
                    rp = self._cmd(op | 0x05, 0, [desc, "", "", "", "", str(codigo), fijo(monto, 2)], T_NORMAL)
                    if not rp.ok and codigo not in (99, 8):
                        registro.log(f"Aviso: el impresor rechazo el medio de pago {codigo} ({rp.error_texto}); "
                                     "se reintenta como 99")
                        rp = self._cmd(op | 0x05, 0, [desc, "", "", "", "", "99", fijo(monto, 2)], T_NORMAL)
                    if not rp.ok:
                        raise ErrorImpresor(f"El impresor fiscal rechazo el pago '{p.descripcion}': {rp.error_texto}",
                                            rp.error)

                # Leyenda de transparencia fiscal (IVA contenido) en los tiques a consumidor final.
                riva = None if es_nc else self._cmd(op | 0x0B, 1, NADA, T_NORMAL)
                if fam == 0x0A and riva is not None and riva.ok:
                    monto = pesos(centavos(riva.campo(0)))
                    linea = "Iva contenido:" + monto.rjust(ANCHO_PAPEL - 14)
                    self._tolerar("cola 1", 0x050A, 0, ["1", "\\n************ TRANSPARENCIA FISCAL **************"])
                    self._tolerar("cola 2", 0x050A, 0, ["2", linea])
                    self._tolerar("cola 3", 0x050A, 0, ["3", "."])
                    self._tolerar("cola 4", 0x050A, 0, ["4", ""])
                else:
                    for i in range(1, 5):
                        self._tolerar(f"limpiar cola {i}", 0x050A, 0, [str(i), ""])

                campos_cierre = [""] * 6 if fam == 0x0B else [""] * 6 + ["0"]
                ext_cierre = 0x0103 if fam == 0x0D else 0x0107
                try:
                    cierre = self._cmd(op | 0x06, ext_cierre, campos_cierre, T_CIERRE)
                except ErrorComunicacion as ex:
                    abierto = False
                    raise self._resolver_cierre_incierto(c, fam, numero, ex) from ex
                if not cierre.ok:
                    raise ErrorImpresor("El impresor fiscal rechazo el cierre del comprobante: " + cierre.error_texto,
                                        cierre.error)
                abierto = False

                res = ResultadoEmision(tipo=tipo_texto)
                if fam == 0x0A:
                    res.numero = cierre.campo(0)
                    res.total, res.iva, res.vuelto = (centavos(cierre.campo(i)) for i in (1, 2, 3))
                else:
                    res.numero, res.letra = cierre.campo(0), cierre.campo(1)
                    res.total, res.iva, res.vuelto = (centavos(cierre.campo(i)) for i in (2, 3, 4))
                if not res.numero:
                    res.numero = numero

                registro.anotar(Emitido(Nombre=c.nombre, Numero=res.numero, Letra=res.letra,
                                        Total=str(redondear(res.total, 2)), Tipo=tipo_texto))
                registro.log(f"EMITIDO pedido '{c.nombre}': {tipo_texto} {(res.letra + ' ' + res.numero).strip()} "
                             f"total {pesos(res.total)} iva {pesos(res.iva)}")
                if abs(res.total - total_pedido) > Decimal("0.05"):
                    registro.log(f"ATENCION: el total del impresor ({pesos(res.total)}) difiere del calculado "
                                 f"con el pedido ({pesos(total_pedido)})")

                try:
                    if fam == 0x0A:
                        for i in range(1, 4):
                            self._tolerar(f"limpiar cola {i}", 0x050A, 0, [str(i), ""])
                    elif fam == 0x0B:
                        self._tolerar("limpiar encabezado 9", 0x0508, 0, ["9", ""])
                        self._tolerar("limpiar encabezado 10", 0x0508, 0, ["10", ""])
                except Exception as ex:  # noqa: BLE001
                    registro.log("Aviso: no se pudo limpiar las lineas despues del cierre: " + str(ex))
                return res
            except Exception as ex:
                if abierto:
                    try:
                        a = self.transporte.enviar(op | 0x07, 0, NADA, T_CIERRE)
                        registro.log("Comprobante anulado tras el error (" + ("ok" if a.ok else a.error_texto) + ")")
                    except Exception as ex2:  # noqa: BLE001
                        registro.log("No se pudo anular el comprobante abierto: " + str(ex2))
                registro.log(f"ERROR pedido '{c.nombre}': {ex}")
                raise

    def _resolver_cierre_incierto(self, c: Comprobante, fam: int, numero: str, original: Exception) -> Exception:
        """El cierre no contesto: averiguar si el comprobante quedo abierto (se anula) o si ya salio."""
        try:
            a = self.transporte.enviar((fam << 8) | 0x07, 0, NADA, T_CIERRE)
            if a.ok:
                return ErrorComunicacion(
                    "El impresor no contesto al cerrar el comprobante. Se anulo y no se emitio nada: se puede reintentar.")
            if a.error == 0x0101:
                detalle = f"Es probable que el comprobante {numero} haya salido."
            else:
                detalle = f"El impresor devolvio {a.error_texto}."
        except Exception as ex:  # noqa: BLE001
            detalle = f"El impresor sigue sin responder ({ex})."
        registro.anotar(Emitido(Nombre=c.nombre, Estado="incierto", Numero=numero, Total=str(redondear(abs(c.total), 2))))
        return ErrorComunicacion(f"No se pudo confirmar si el comprobante salio. {detalle} "
                                 f"Revisar el papel antes de reintentar. ({original})")

    # --- cierres y estado --------------------------------------------------------------------

    def cierre(self, tipo: Optional[str]) -> str:
        with self._candado:
            tipo = (tipo or "").strip().lower()
            if tipo == "z":
                self._anular_pendientes()
                r = self._exigir("el cierre Z", 0x0801, 0, NADA, T_ZETA)
                registro.log("CIERRE Z numero " + r.campo(0))
                return r.campo(0)
            if tipo == "x":
                r = self._exigir("el informe X", 0x0803, 0, NADA, T_ZETA)
                registro.log("INFORME X numero " + r.campo(0))
                return r.campo(0)
            raise ErrorPedido(f"Tipo de cierre fiscal desconocido: '{tipo}' (se espera x o z)")

    def estado(self) -> Respuesta:
        with self._candado:
            r = self._cmd(0x0001, 0, NADA, 3000)
            if not r.ok:
                raise ErrorImpresor("El impresor fiscal informa: " + r.error_texto, r.error)
            return r
