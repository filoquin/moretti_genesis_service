"""Comprobante tal como lo manda el POS de Odoo (mismo JSON que el parametro "vals" de pos_proxy_service).

Portado de genesis-fiscal (Comprobante.cs). Los importes son Decimal: nunca float.
"""
import json
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, List, Optional

from .protocolo import ErrorPedido, limpiar

CERO = Decimal(0)


def redondear(v: Decimal, decimales: int) -> Decimal:
    """Redondeo half away from zero (como MidpointRounding.AwayFromZero de .NET)."""
    return v.quantize(Decimal(1).scaleb(-decimales), rounding=ROUND_HALF_UP)


@dataclass
class Item:
    descripcion: str = ""
    cantidad: Decimal = CERO
    precio: Decimal = CERO  # unitario, con impuestos y con el descuento de linea aplicado
    iva: Decimal = CERO     # porcentaje

    @property
    def importe(self) -> Decimal:
        return self.cantidad * self.precio


@dataclass
class Pago:
    codigo: int = 99
    monto: Decimal = CERO
    descripcion: str = ""


@dataclass
class Cliente:
    nombre1: str = ""
    nombre2: str = ""
    domicilio1: str = ""
    domicilio2: str = ""
    domicilio3: str = ""
    tipo_documento: str = ""
    documento: str = ""
    responsabilidad: str = ""
    asociado1: str = ""
    asociado2: str = ""
    asociado3: str = ""
    cheque: str = ""


@dataclass
class Comprobante:
    nombre: str = ""
    tipo: int = 83
    cliente: Cliente = field(default_factory=Cliente)
    items: List[Item] = field(default_factory=list)
    pagos: List[Pago] = field(default_factory=list)
    descuento_informado: Decimal = CERO

    @property
    def es_tique(self) -> bool:
        return self.tipo == 83

    @property
    def total(self) -> Decimal:
        return sum((redondear(i.importe, 2) for i in self.items), CERO)

    @property
    def es_nota_credito(self) -> bool:
        """El tipo 110 del modulo viejo o cualquier pedido con total negativo (reembolso de Odoo)."""
        return self.tipo == 110 or self.total < 0

    # --- lectura ---------------------------------------------------------------------------

    @classmethod
    def desde_json(cls, texto) -> "Comprobante":
        if isinstance(texto, (bytes, bytearray)):
            texto = texto.decode("utf-8")
        try:
            raiz = json.loads(texto, parse_float=Decimal)
        except ValueError as ex:
            raise ErrorPedido("Los valores del tique no son un JSON valido: " + str(ex)) from ex
        return cls.desde_dict(raiz)

    @classmethod
    def desde_dict(cls, raiz: Any) -> "Comprobante":
        if not isinstance(raiz, dict):
            raise ErrorPedido("Formato de los valores incorrecto")
        c = cls()
        c.nombre = limpiar(_str(raiz, "name"))
        if c.nombre == "/":
            c.nombre = ""
        tipo = _dec(raiz, "type")
        if tipo is not None:
            c.tipo = int(tipo)

        dc = raiz.get("cliente")
        if isinstance(dc, dict):
            cl = c.cliente
            cl.nombre1 = _str(dc, "nombre_o_razon_social1")
            cl.nombre2 = _str(dc, "nombre_o_razon_social2")
            cl.domicilio1 = _str(dc, "domicilio1")
            cl.domicilio2 = _str(dc, "domicilio2")
            cl.domicilio3 = _str(dc, "domicilio3")
            cl.tipo_documento = _str(dc, "id_tipo_documento").upper()
            cl.documento = "".join(ch for ch in _str(dc, "numero_documento") if ch.isalnum())
            cl.responsabilidad = _str(dc, "id_responsabilidad_iva").upper()
            cl.asociado1 = _str(dc, "documento_asociado1")
            cl.asociado2 = _str(dc, "documento_asociado2")
            cl.asociado3 = _str(dc, "documento_asociado3")
            cl.cheque = _str(dc, "cheque_reintegro_turista")

        for n, di in enumerate(_lista(raiz, "items"), start=1):
            if not isinstance(di, dict):
                raise ErrorPedido(f"El item {n} no tiene el formato esperado")
            it = Item(descripcion=_str(di, "description"))
            extra = _str(di, "description_extra1")
            if extra:
                it.descripcion = (it.descripcion + " " + extra).strip()
            cantidad = _dec(di, "qty")
            if cantidad is None:
                raise ErrorPedido(f"El item {n} ({it.descripcion}) no tiene cantidad")
            precio = _dec(di, "price")
            if precio is None:
                raise ErrorPedido(f"No se pudo leer el precio del item {n} ({it.descripcion})")
            it.cantidad, it.precio = cantidad, precio
            it.iva = _dec(di, "iva") or CERO
            c.items.append(it)

        for dp in _lista(raiz, "pagos"):
            if not isinstance(dp, dict):
                continue
            p = Pago()
            cod = _dec(dp, "codigo_forma_pago")
            if cod is not None:
                p.codigo = int(cod)
            p.monto = _dec(dp, "monto") or CERO
            p.descripcion = _str(dp, "descripcion")
            c.pagos.append(p)

        for dd in _lista(raiz, "descuentos"):
            m = _dec(dd, "monto") if isinstance(dd, dict) else None
            if m is not None:
                c.descuento_informado += m
        return c

    # --- validacion ------------------------------------------------------------------------

    def validar(self) -> None:
        """Deja el comprobante listo para emitir o explica por que no se puede."""
        if self.tipo not in (110, 83, 81, 82, 111):
            raise ErrorPedido(f"Tipo de comprobante no soportado: {self.tipo}")
        if not self.items:
            raise ErrorPedido("El pedido no tiene items. No se emitio nada.")
        for it in self.items:
            if abs(it.cantidad) > 99999:
                raise ErrorPedido(f"Cantidad fuera de rango en '{it.descripcion}'")
            if abs(it.precio) > 9999999:
                raise ErrorPedido(f"Precio fuera de rango en '{it.descripcion}'")
            if it.iva < 0 or it.iva > 99:
                raise ErrorPedido(f"Tasa de IVA invalida en '{it.descripcion}'")
        if self.total == 0:
            raise ErrorPedido("El pedido tiene total cero. No se emitio nada.")


def _str(d: dict, clave: str) -> str:
    v = d.get(clave) if isinstance(d, dict) else None
    if v is None or isinstance(v, bool):
        return ""
    if isinstance(v, Decimal):
        v = format(v.normalize(), "f") if v == v.to_integral_value() else str(v)
    return str(v).strip()


def _dec(d: dict, clave: str) -> Optional[Decimal]:
    v = d.get(clave) if isinstance(d, dict) else None
    if v is None or isinstance(v, bool):
        return None
    try:
        if isinstance(v, str):
            s = v.strip()
            r = Decimal(s) if s else None
        elif isinstance(v, (int, Decimal)):
            r = Decimal(v)
        elif isinstance(v, float):
            r = Decimal(repr(v))
        else:
            return None
    except (InvalidOperation, ValueError):
        return None
    return r if r is not None and r.is_finite() else None


def _lista(d: dict, clave: str) -> list:
    v = d.get(clave)
    return v if isinstance(v, list) else []
