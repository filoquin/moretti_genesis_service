# moretti-genesis-service

Servicio local que recibe pedidos del POS de Odoo y los emite en el controlador fiscal **Moretti
Genesis**, hablando su protocolo nativo por el puerto serie USB.

Es un port a Python de `genesis-proxy` (repo `juangiro/genesis-fiscal`, rama `firma-hmac`), que se
probó contra el impresor real el 12/11/2026. **La lógica fiscal es la misma**: misma secuencia de
comandos, mismos campos, mismas reglas. Hay una prueba que lo compara comando por comando
(ver Pruebas). Lo que agrega: una API JSON propia y dos arreglos del transporte serie.

Estado: probado contra el simulador y contra la implementación C#. **No se probó todavía en el
impresor real.**

## Instalar y correr

```
pip install -r requirements.txt
python app.py --simular                 # sin impresor: no emite nada
python app.py --probar                  # consulta el estado del impresor (solo lectura) y termina
python app.py [--config archivo.ini] [--puerto-http N]
```

Configuración: `genesis-proxy.ini` al lado de `app.py` (ver `genesis-proxy.ejemplo.ini`); cada clave se
puede pisar con una variable de entorno (`FISCAL_SERIAL_PORT`, `FLASK_PORT`, etc., ver
`genesis_service/config.py`). Log y registro de emitidos en `%ProgramData%\GenesisProxy`, con el
mismo formato que `genesis-proxy`.

Para una caja Windows sin Python: `pyinstaller --onefile app.py` deja un único `.exe`.

El puerto serie es exclusivo: para usar el impresor real hay que detener ProtoEmul y pausar la tarea
`CajaWatchdog`; para escuchar en 5005, detener `server.py`.

## API JSON

El cuerpo de `/imprimir_ticket` es el mismo JSON que arma el POS de Odoo (`get_values_ticket`), más
`"forzar": true` opcional para saltear el control de duplicados.

| Ruta | Cuerpo | Respuesta OK |
|---|---|---|
| `POST /imprimir_ticket` | comprobante | `{"ok": true, "comprobante": {numero, letra, tipo, total, iva, vuelto, simulado}}` |
| `POST /cierre_x` | vacío | `{"ok": true, "tipo": "x", "numero": "..."}` |
| `POST /cierre_z` | vacío | `{"ok": true, "tipo": "z", "numero": "..."}` |
| `GET /health` | | `{"ok": true, "service", "version", "simulado"}` |
| `GET /estado` | | diagnóstico del servicio y del impresor |

Errores: `{"ok": false, "error": "<texto>", "categoria": ...}` con

| Código | `categoria` | Cuándo |
|---|---|---|
| 400 | `pedido` | JSON inválido, validación, NC sin comprobante asociado, cierre de tipo desconocido |
| 409 | `duplicado` | el pedido ya se emitió, o quedó incierto |
| 422 | `impresor` | el impresor rechazó un paso (`codigo_impresor` = código Moretti) |
| 503 | `comunicacion` | sin respuesta, puerto, formato; en un cierre incierto dice qué revisar |
| 403 | `seguridad` | origen no permitido, firma ausente o inválida |
| 500 | `interno` | |

### Rutas compatibles con `pos_proxy_service`

Las de hoy, para usar el servicio sin tocar Odoo: `GET /print_pos_ticket?vals=<json>[&forzar=1]`,
`GET /print_pos_fiscal_close?type=x|z`, `GET /state_printer`. Contestan `{"response": true | "<error>"}`
con HTTP 200 (403 solo por seguridad), igual que `genesis-proxy`.

## Seguridad

Dos controles, en el `.ini` (detalle en `genesis_service/seguridad.py`):

- **`origenes`**: si el pedido trae `Origin` y no está en la lista, 403. Sin `Origin` pasa.
- **`clave_firma`**: los pedidos que emiten algo van firmados con HMAC-SHA256 y la clave de la caja.
  - API JSON, en cabeceras:
    ```
    X-Genesis-Ts:    <segundos Unix UTC>
    X-Genesis-Firma: hex(HMAC_SHA256(clave, "POST " + ruta + "\n" + ts + "\n" + cuerpo))
    ```
  - Rutas GET, en la consulta (igual que `genesis-proxy` y `pos_proxy_service` rama `19.0-firma-proxy`):
    ```
    firma = hex(HMAC_SHA256(clave, ruta + "?" + consulta_con_ts))    URL = ruta?consulta_con_ts&firma=<firma>
    ```
  Se rechaza firma inválida, `ts` a más de `firma_ventana_seg` (120 s) y firmas repetidas. Como la
  ruta entra en la firma, una firma de cierre X no sirve para un Z.

## Diferencias con `genesis-proxy` (C#)

1. **API JSON** (arriba), además de las rutas compatibles.
2. **Reenvío ante NAK**: si el impresor contesta NAK, se reenvía la trama (hasta 3 veces). En
   `genesis-proxy` se esperaba el timeout y se anulaba el comprobante.
3. **Secuencia incremental opcional** (`secuencia_incremental = si`): número de secuencia `81`..`FE` y
   se descartan las respuestas que no son del comando enviado. Evita que una respuesta atrasada del
   cierre se tome como respuesta a la anulación (comprobante duplicado). Por defecto sigue fija en
   `A9`, que es lo probado en el impresor: **hay que probarla antes de activarla**.
4. **Número de pedido recortado** a 28 caracteres en la primera línea del primer ítem. En
   `genesis-proxy` un nombre largo hacía rechazar el ítem.
5. Detección del CH340 con `pyserial` (`list_ports`), sin WMI: anda en Windows y en Linux.

Siguen igual (defectos conocidos, ver la spec en el harness de Casa Goro,
`specs/10_draft/genesis-proxy.md`): control de duplicados por `name`, tiempos que pueden superar los
100 s del POS, `iva` faltante como 0 gravado, cajón antes del cierre.

## Pruebas

```
python -m pytest tests          # protocolo, API JSON, firma, transporte serie con un puerto falso
```

Paridad con `genesis-proxy`: levantar los dos en modo simulado, con la carpeta de datos vacía y
`log_detallado = si`, y correr

```
python tests/paridad.py http://127.0.0.1:5101 <log del C#> http://127.0.0.1:5102 <log de este>
```

Manda los 31 pedidos de `tests/casos_paridad.py` (tique, factura A/B, cambio, NC consumidor final,
NC A/B, tipo 110, duplicados, errores, cierres) y compara los comandos enviados al impresor y las
respuestas. Resultado al 13/11/2026: 31 de 31 iguales. Las tramas (escapes y checksum) y la partición
de descripciones largas también se compararon byte a byte contra `Protocolo.cs`.
