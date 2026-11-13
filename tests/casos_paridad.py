"""Pedidos para comparar este servicio con genesis-fiscal (C#), comando por comando.

Cada caso es (nombre, ruta, parametros). Se mandan en orden y por las rutas compatibles (GET), que
son el contrato comun de las dos implementaciones. Ver tests/paridad.py.
"""
CF = {}
CLIENTE_A = {"nombre_o_razon_social1": "Ferreteria El Tornillo SRL", "domicilio1": "Av. Alberdi 1234",
             "id_tipo_documento": "T", "numero_documento": "30-71234567-9", "id_responsabilidad_iva": "I"}
CLIENTE_DNI = {"nombre_o_razon_social1": "Juana Perez", "domicilio1": "", "id_tipo_documento": "D",
               "numero_documento": "20.123.456", "id_responsabilidad_iva": "F"}


def item(desc, qty, price, iva=21, extra=""):
    return {"description": desc, "description_extra1": extra, "qty": qty, "price": price, "iva": iva,
            "unit_measure": "7", "code_intern": "11111", "product_discount_general": False}


def pago(monto, cod=99, desc="Otros"):
    return {"codigo_forma_pago": cod, "cantidad_cuotas": "", "monto": monto, "descripcion_cupones": "",
            "descripcion": desc, "descripcion_extra1": "", "descripcion_extra2": ""}


def ticket(name, typ, cliente, items, pagos, descuentos=0):
    return {"name": name, "type": typ, "cliente": cliente, "items": items, "pagos": pagos,
            "descuentos": [{"descripcion": "Descuentos", "monto": descuentos}], "ajustes": []}


CASOS = [
    ("tique simple con vuelto", "/print_pos_ticket",
     ticket("Shop/0001", 83, CF, [item("LUZ LED P/ BICICLETA", 1, 5500)], [pago(6000, 8, "Efectivo")])),
    ("tique nombre largo, tres pagos, 20 y 21", "/print_pos_ticket",
     ticket("Shop/0002", 83, CF,
            [item("LINTERNA MINERO 1 W. ALTA POTENCIA RECARGABLE CON CARGADOR USB", 2, 2500),
             item("Pila AA", 4, 350.5, 21)],
            [pago(2000, 20, "Visa Credito"), pago(1000, 21, "Debito Maestro"), pago(3000, 8, "Efectivo")])),
    ("cambio: venta y devolucion, total positivo", "/print_pos_ticket",
     ticket("Shop/0003", 83, CF, [item("Pelota de futbol N5", 1, 5500), item("Pelota de basquet", -1, 2500)],
            [pago(3000, 8, "Efectivo")])),
    ("factura B consumidor final sin documento, cantidad decimal e IVA 10,5", "/print_pos_ticket",
     ticket("Shop/0004", 82, {"id_responsabilidad_iva": "F", "numero_documento": ""},
            [item("Pan de molde integral", 1.5, 1200, 10.5), item("Gaseosa 2,25 l", 1, 2100)],
            [pago(3900, 99, "MP Point - C3ALB")])),
    ("factura A con CUIT", "/print_pos_ticket",
     ticket("Shop/0005", 81, CLIENTE_A, [item("Taladro percutor 650W", 1, 89999.99)], [pago(89999.99, 99, "Transferencia")])),
    ("NC consumidor final con linea de signo contrario", "/print_pos_ticket",
     ticket("Shop/0006", 83, CF, [item("Mate de calabaza", -1, 5500), item("Bombilla", 1, 1000)],
            [pago(-4500, 8, "Efectivo")])),
    ("NC B con comprobante asociado", "/print_pos_ticket",
     ticket("Shop/0007", 82, dict(CLIENTE_DNI, documento_asociado1="082-00010-00000359"),
            [item("Termo 1 l", -1, 15000)], [pago(-15000, 99, "Otros")])),
    ("NC A con comprobante asociado", "/print_pos_ticket",
     ticket("Shop/0008", 81, dict(CLIENTE_A, documento_asociado1="081-00010-00000563"),
            [item("Amoladora 115mm", -2, 45000.5)], [pago(-90001, 99, "Transferencia")])),
    ("NC a cliente identificado sin comprobante asociado: error", "/print_pos_ticket",
     ticket("Shop/0009", 82, CLIENTE_DNI, [item("Termo 1 l", -1, 15000)], [pago(-15000)])),
    ("NC A/B sin documento ni asociado: cae a NC consumidor final", "/print_pos_ticket",
     ticket("Shop/0010", 82, {"id_responsabilidad_iva": "F"}, [item("Taza", -1, 3000)], [pago(-3000, 8)])),
    ("tipo 110 con total positivo", "/print_pos_ticket",
     ticket("Shop/0011", 110, CF, [item("Devolucion yerba", 1, 4200)], [pago(4200, 8)])),
    ("duplicado del tique simple", "/print_pos_ticket", ticket("Shop/0001", 83, CF, [item("LUZ LED", 1, 5500)], [pago(5500, 8)])),
    ("duplicado con forzar=1", "/print_pos_ticket", ("forzar", ticket("Shop/0001", 83, CF, [item("LUZ LED", 1, 5500)], [pago(5500, 8)]))),
    ("total cero", "/print_pos_ticket", ticket("Shop/0012", 83, CF, [item("A", 1, 100), item("B", -1, 100)], [pago(0)])),
    ("tipo no soportado", "/print_pos_ticket", ticket("Shop/0013", 99, CF, [item("A", 1, 100)], [pago(100)])),
    ("sin items", "/print_pos_ticket", ticket("Shop/0014", 83, CF, [], [pago(100)])),
    ("JSON invalido", "/print_pos_ticket", "{esto no es json"),
    ("vals vacio", "/print_pos_ticket", ""),
    ("codigos de pago invalidos, monto cero y descuentos informados", "/print_pos_ticket",
     ticket("Shop/0015", 83, CF, [item("Cuaderno A4", 3, 1999.99)],
            [pago(0, 8, "Efectivo"), pago(3000, 50, "Codigo raro"), pago(2999.97, 0, "Cero")], descuentos=150.25)),
    ("iva ausente y textos con acentos", "/print_pos_ticket",
     ticket("Shop/0016", 83, CF, [dict(item("Ñandú café º 1ª calidad", 1, 999), iva=False)], [pago(999, 8)])),
    ("numeros como texto", "/print_pos_ticket",
     ticket("Shop/0017", 83, CF, [item("Clavos 2 pulgadas x kg", "0.750", "4800.00", "21")], [pago("3600", "8")])),
    ("item sin cantidad", "/print_pos_ticket", ticket("Shop/0018", 83, CF, [{"description": "x", "price": 1}], [])),
    ("item con importe cero entre otros", "/print_pos_ticket",
     ticket("Shop/0019", 83, CF, [item("Regalo", 1, 0), item("Lapiz", 2, 300)], [pago(600, 8)])),
    ("name barra: sin control de duplicado (1)", "/print_pos_ticket", ticket("/", 83, CF, [item("Goma", 1, 150)], [pago(150, 8)])),
    ("name barra: sin control de duplicado (2)", "/print_pos_ticket", ticket("/", 83, CF, [item("Goma", 1, 150)], [pago(150, 8)])),
    ("cliente con tipo y responsabilidad desconocidos", "/print_pos_ticket",
     ticket("Shop/0020", 82, {"nombre_o_razon_social1": "", "id_tipo_documento": "ZZ", "numero_documento": "12345678",
                              "id_responsabilidad_iva": "Q", "domicilio1": "Calle Falsa 123 " * 4},
            [item("Silla plegable de aluminio reforzada para camping", 2, 32500)], [pago(65000)])),
    ("informe X", "/print_pos_fiscal_close", {"type": "x"}),
    ("cierre Z", "/print_pos_fiscal_close", {"type": "Z"}),
    ("cierre de tipo desconocido", "/print_pos_fiscal_close", {"type": "q"}),
    ("cierre sin tipo", "/print_pos_fiscal_close", {}),
    ("estado", "/state_printer", {}),
]
