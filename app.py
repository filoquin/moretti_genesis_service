"""moretti-genesis-service: servicio local para el controlador fiscal Moretti Genesis.

Uso: python app.py [--config archivo.ini] [--simular] [--puerto-http N] [--probar]
"""
import os
import sys

from genesis_service import registro
from genesis_service.config import Config
from genesis_service.genesis import Genesis
from genesis_service.http import SERVICIO, VERSION, crear_app
from genesis_service.seguridad import Seguridad
from genesis_service.transporte import TransporteSerie, TransporteSimulado

AYUDA = f"""{SERVICIO} {VERSION}
Uso: python app.py [--config archivo.ini] [--simular] [--puerto-http N] [--probar]
  --config X       archivo de configuracion (por defecto genesis-proxy.ini al lado de app.py)
  --simular        no abre el puerto serie: responde un impresor simulado (no emite nada)
  --puerto-http N  puerto en el que escucha (por defecto el del .ini, o 5005)
  --probar         consulta el estado del impresor (solo lectura) y termina"""


def construir(cfg: Config):
    transporte = TransporteSimulado() if cfg.simular else TransporteSerie(cfg.puerto_serie, cfg.baudios,
                                                                         cfg.secuencia_incremental)
    genesis = Genesis(transporte, cfg.abrir_cajon)
    seguridad = Seguridad(cfg.clave_firma, cfg.origenes, cfg.firma_ventana_seg)
    return transporte, genesis, seguridad


def main(argv) -> int:
    base = os.path.dirname(os.path.abspath(getattr(sys, "frozen", False) and sys.executable or __file__))
    archivo = os.path.join(base, "genesis-proxy.ini")
    simular = probar = False
    puerto_http = None
    i = 0
    while i < len(argv):
        a = argv[i].lower()
        if a == "--simular":
            simular = True
        elif a == "--probar":
            probar = True
        elif a == "--config" and i + 1 < len(argv):
            i += 1
            archivo = argv[i]
        elif a == "--puerto-http" and i + 1 < len(argv):
            i += 1
            puerto_http = int(argv[i])
        else:
            print(AYUDA)
            return 0 if a in ("--ayuda", "-h", "--help") else 2
        i += 1

    cfg = Config.cargar(archivo)
    if simular:
        cfg.simular = True
    if puerto_http:
        cfg.puerto_http = puerto_http

    try:
        registro.iniciar(cfg.carpeta_datos, cfg.log_detallado, cfg.simular)
    except OSError as ex:
        print(f"No se pudo usar la carpeta de datos {cfg.carpeta_datos}: {ex}", file=sys.stderr)
        return 1

    transporte, genesis, seguridad = construir(cfg)

    if probar:
        try:
            r = genesis.estado()
            print(f"Impresor OK por {genesis.canal}: estado impresora {r.estado_impresora:04X}, "
                  f"estado fiscal {r.estado_fiscal:04X}")
            return 0
        except Exception as ex:  # noqa: BLE001
            print("FALLO: " + str(ex))
            return 1
        finally:
            transporte.cerrar()

    registro.log(f"=== {SERVICIO} {VERSION} iniciado. Impresor: {transporte.descripcion}. "
                 f"Config: {cfg.archivo or '(valores por defecto)'}. Datos: {cfg.carpeta_datos} ===")
    registro.log("Firma: " + (f"exigida (ventana {seguridad.ventana_seg} s)" if seguridad.exige_firma
                              else "NO exigida, falta clave_firma") +
                 ". Origenes: " + (seguridad.origenes if seguridad.controla_origen else "cualquiera, falta origenes") +
                 ". Secuencia: " + ("incremental" if cfg.secuencia_incremental else "fija A9"))
    app = crear_app(genesis, seguridad, cfg.simular)
    registro.log(f"Escuchando en http://{cfg.escuchar}:{cfg.puerto_http}" + ("  [MODO SIMULADO]" if cfg.simular else ""))
    try:
        from waitress import serve
        serve(app, host=cfg.escuchar, port=cfg.puerto_http, threads=8, ident=SERVICIO)
    except ImportError:
        app.run(host=cfg.escuchar, port=cfg.puerto_http, threaded=True)
    except OSError as ex:
        registro.log(f"No se pudo escuchar en {cfg.escuchar}:{cfg.puerto_http}: {ex} (hay otro programa usando ese puerto?)")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
