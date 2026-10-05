import json
import tempfile
import unittest
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from radar import monitor
from radar.fuentes import ApiCompraAgil, ApiLicitaciones, ClienteHTTP, CuotaAgotada, fecha_iso
from radar.monitor import RUTA_TERMINOS, Radar
from radar.terminos import Diccionario

AHORA = datetime(2026, 10, 5, 15, 0, tzinfo=timezone.utc)
DICC = Diccionario.desde_archivo(RUTA_TERMINOS)


def lic_resumen(codigo, nombre, estado=5, cierre="2026-10-20T15:00:00"):
    return {"CodigoExterno": codigo, "Nombre": nombre, "CodigoEstado": estado, "FechaCierre": cierre}


def lic_detalle(codigo, nombre, descripcion="", items=(), estado=5):
    return {
        **lic_resumen(codigo, nombre, estado),
        "Descripcion": descripcion,
        "Comprador": {"NombreOrganismo": "MUNICIPALIDAD DE PRUEBA", "RegionUnidad": "Región de Valparaíso"},
        "Fechas": {"FechaPublicacion": "2026-10-01T10:00:00", "FechaCierre": "2026-10-20T15:00:00"},
        "MontoEstimado": 12000000,
        "Moneda": "CLP",
        "Items": {"Listado": [{"NombreProducto": n, "Descripcion": d} for n, d in items]},
    }


def ca_item(codigo, nombre, estado="publicada", cambio="2026-10-05T12:00:00Z"):
    return {
        "codigo": codigo,
        "nombre": nombre,
        "estado": {"codigo": estado, "glosa": estado.capitalize()},
        "fechas": {
            "fecha_publicacion": "2026-10-04T09:00:00Z",
            "fecha_cierre": "2026-10-08T18:00:00Z",
            "fecha_ultimo_cambio": cambio,
        },
        "montos": {"moneda": "CLP", "monto_disponible": 900000, "monto_disponible_clp": 900000},
        "institucion": {"organismo_comprador": "SERVICIO DE PRUEBA", "nombre_region": "Metropolitana"},
        "resumen": {"total_ofertas_recibidas": 2},
    }


class ApiFalsa:
    """Responde según la URL, como lo harían las APIs reales."""

    def __init__(self, activas=(), detalles_lic=None, busqueda_ca=None, detalles_ca=None):
        self.activas = list(activas)
        self.detalles_lic = detalles_lic or {}
        self.busqueda_ca = busqueda_ca or {}
        self.detalles_ca = detalles_ca or {}
        self.urls = []

    def __call__(self, url, headers=None):
        self.urls.append(url)
        partes = urllib.parse.urlparse(url)
        q = dict(urllib.parse.parse_qsl(partes.query))
        if "licitaciones.json" in partes.path:
            assert q.get("ticket") == "T"
            if q.get("estado") == "activas":
                return {"Cantidad": len(self.activas), "Listado": self.activas}
            d = self.detalles_lic.get(q["codigo"])
            return {"Cantidad": 1 if d else 0, "Listado": [d] if d else []}
        assert headers == {"ticket": "T"}
        if partes.path.endswith("/v2/compra-agil"):
            items = self.busqueda_ca.get(q["q"], [])
            return {"success": "OK", "payload": {"items": items, "paginacion": {"total_paginas": 1 if items else 0}}}
        codigo = urllib.parse.unquote(partes.path.rsplit("/", 1)[1])
        if codigo not in self.detalles_ca:
            raise urllib.error.HTTPError(url, 404, "Not found", {}, None)
        return {"success": "OK", "payload": self.detalles_ca[codigo]}


def crear_radar(api, previos=None, cache=None, ahora=AHORA):
    http = ClienteHTTP(obtener=api, dormir=lambda s: None)
    return Radar(
        DICC,
        ApiLicitaciones("T", http),
        ApiCompraAgil("T", http),
        previos or {},
        cache or {},
        ahora,
    )


class TestLicitaciones(unittest.TestCase):
    def test_detecta_por_nombre_y_por_descripcion(self):
        api = ApiFalsa(
            activas=[
                lic_resumen("1-1-LE26", "ADQUISICION DE DRON"),
                lic_resumen("2-2-LE26", "EQUIPAMIENTO TECNOLOGICO"),
                lic_resumen("3-3-LE26", "SERVICIO DE ASEO"),
            ],
            detalles_lic={
                "1-1-LE26": lic_detalle("1-1-LE26", "ADQUISICION DE DRON"),
                "2-2-LE26": lic_detalle("2-2-LE26", "EQUIPAMIENTO TECNOLOGICO", items=[("Aeronave", "RPAS con cámara térmica")]),
                "3-3-LE26": lic_detalle("3-3-LE26", "SERVICIO DE ASEO"),
            },
        )
        datos, cache = crear_radar(api).ejecutar()
        ids = {i["id"]: i for i in datos["items"]}
        self.assertEqual(set(ids), {"LIC:1-1-LE26", "LIC:2-2-LE26"})
        self.assertEqual(ids["LIC:2-2-LE26"]["terminos"], ["RPA / RPAS"])
        self.assertEqual(ids["LIC:1-1-LE26"]["organismo"], "MUNICIPALIDAD DE PRUEBA")
        self.assertEqual(ids["LIC:1-1-LE26"]["fecha_cierre"], "2026-10-20T15:00:00-03:00")
        self.assertIn("idlicitacion=1-1-LE26", ids["LIC:1-1-LE26"]["url"])
        self.assertEqual(sorted(datos["nuevas_en_esta_ejecucion"]), ["LIC:1-1-LE26", "LIC:2-2-LE26"])
        self.assertEqual(set(cache["lic_revisadas"]), {"1-1-LE26", "2-2-LE26", "3-3-LE26"})
        self.assertTrue(datos["fuentes"]["licitaciones"]["ok"])

    def test_no_repite_detalles_ya_revisados(self):
        api = ApiFalsa(
            activas=[lic_resumen("3-3-LE26", "SERVICIO DE ASEO")],
            detalles_lic={"3-3-LE26": lic_detalle("3-3-LE26", "SERVICIO DE ASEO")},
        )
        _, cache = crear_radar(api).ejecutar()
        api.urls.clear()
        crear_radar(api, cache=cache, ahora=AHORA + timedelta(hours=1)).ejecutar()
        self.assertFalse(any("codigo=" in u for u in api.urls if "licitaciones" in u))

    def test_conserva_primera_deteccion_y_marca_cambio_estado(self):
        api = ApiFalsa(
            activas=[lic_resumen("1-1-LE26", "COMPRA DE DRONES")],
            detalles_lic={"1-1-LE26": lic_detalle("1-1-LE26", "COMPRA DE DRONES")},
        )
        datos, cache = crear_radar(api).ejecutar()
        # Una semana después ya no está activa y fue adjudicada.
        api.activas = []
        api.detalles_lic["1-1-LE26"] = lic_detalle("1-1-LE26", "COMPRA DE DRONES", estado=8)
        despues = AHORA + timedelta(days=7)
        datos2, _ = crear_radar(api, previos=datos, cache=cache, ahora=despues).ejecutar()
        item = datos2["items"][0]
        self.assertEqual(item["estado"], "adjudicada")
        self.assertEqual(item["primera_deteccion"], AHORA.isoformat())
        self.assertEqual(item["cambio_estado"], despues.isoformat())
        self.assertEqual(datos2["nuevas_en_esta_ejecucion"], [])

    def test_respeta_tope_de_detalles(self):
        activas = [lic_resumen(f"{n}-1-LE26", f"ARTICULO {n}") for n in range(10)] + [
            lic_resumen("99-1-LE26", "DRON AGRICOLA")
        ]
        api = ApiFalsa(activas=activas, detalles_lic={})
        radar = crear_radar(api)
        radar.cfg.max_detalles_licitaciones = 3
        datos, _ = radar.ejecutar()
        detalles_pedidos = [u for u in api.urls if "codigo=" in u]
        self.assertEqual(len(detalles_pedidos), 3)
        # La que coincide por nombre se revisa primero.
        self.assertIn("codigo=99-1-LE26", detalles_pedidos[0])
        self.assertEqual(datos["fuentes"]["licitaciones"]["pendientes_por_revisar"], 8)


class TestCompraAgil(unittest.TestCase):
    def test_verifica_resultados_del_buscador(self):
        api = ApiFalsa(
            busqueda_ca={
                "dron": [ca_item("100-1-COT26", "Compra de dron para fiscalización")],
                # El buscador también devuelve resultados que no son de drones.
                "RPA": [ca_item("200-1-COT26", "Licencias de software"), ca_item("300-1-COT26", "Equipos varios")],
            },
            detalles_ca={
                "100-1-COT26": {**ca_item("100-1-COT26", "Compra de dron para fiscalización"), "descripcion": "Dron DJI"},
                "200-1-COT26": {**ca_item("200-1-COT26", "Licencias de software"), "descripcion": "RPA UiPath automatización robótica de procesos"},
                "300-1-COT26": {
                    **ca_item("300-1-COT26", "Equipos varios"),
                    "descripcion": "Ver productos",
                    "productos_solicitados": [{"nombre": "Multirrotor", "descripcion": "con cámara"}],
                },
            },
        )
        datos, cache = crear_radar(api).ejecutar()
        ids = {i["id"]: i for i in datos["items"]}
        self.assertEqual(set(ids), {"CA:100-1-COT26", "CA:300-1-COT26"})
        self.assertEqual(ids["CA:100-1-COT26"]["terminos"], ["Dron", "Marcas (DJI, Autel…)"])
        self.assertEqual(ids["CA:100-1-COT26"]["url"], "https://buscador.mercadopublico.cl/ficha?code=100-1-COT26")
        self.assertEqual(ids["CA:100-1-COT26"]["monto"], 900000)
        self.assertIn("200-1-COT26", cache["ca_descartadas"])
        self.assertEqual(datos["fuentes"]["compra_agil"]["modo"], "completo")

        # Segunda ejecución: incremental y sin volver a pedir el detalle descartado.
        api.urls.clear()
        datos2, _ = crear_radar(api, previos=datos, cache=cache, ahora=AHORA + timedelta(hours=1)).ejecutar()
        self.assertEqual(datos2["fuentes"]["compra_agil"]["modo"], "incremental")
        self.assertTrue(all("cambio_desde=" in u for u in api.urls if u.split("?")[0].endswith("/compra-agil")))
        self.assertFalse(any(u.endswith("/200-1-COT26") for u in api.urls))

    def test_cuota_agotada_no_borra_datos(self):
        previos = {"items": [{"id": "CA:1", "fuente": "compra_agil", "codigo": "1", "nombre": "Dron", "estado": "abierta", "terminos": ["Dron"]}]}

        def sin_cuota(url, headers=None):
            if "api2" in url:
                raise urllib.error.HTTPError(url, 429, "Too Many", {}, None)
            return {"Listado": []}

        datos, _ = crear_radar(sin_cuota, previos=previos).ejecutar()
        self.assertFalse(datos["fuentes"]["compra_agil"]["ok"])
        self.assertIn("429", datos["fuentes"]["compra_agil"]["mensaje"])
        self.assertEqual([i["id"] for i in datos["items"]], ["CA:1"])

    def test_cierre_por_fecha(self):
        previos = {
            "items": [
                {"id": "CA:9", "fuente": "compra_agil", "codigo": "9", "nombre": "Dron", "estado": "abierta",
                 "fecha_cierre": "2026-10-01T10:00:00+00:00", "terminos": ["Dron"]}
            ]
        }
        datos, _ = crear_radar(ApiFalsa(), previos=previos).ejecutar()
        self.assertEqual(datos["items"][0]["estado"], "cerrada")


class TestUtilidades(unittest.TestCase):
    def test_fecha_iso(self):
        self.assertEqual(fecha_iso("2026-01-15T15:00:00"), "2026-01-15T15:00:00-03:00")  # horario de verano
        self.assertEqual(fecha_iso("2026-07-15T15:00:00.83"), "2026-07-15T15:00:00-04:00")
        self.assertEqual(fecha_iso("2026-07-15T15:00:00Z"), "2026-07-15T15:00:00+00:00")
        self.assertIsNone(fecha_iso(""))

    def test_429_lanza_cuota_agotada(self):
        def falla(url, headers=None):
            raise urllib.error.HTTPError(url, 429, "Too Many", {}, None)

        with self.assertRaises(CuotaAgotada):
            ClienteHTTP(obtener=falla, dormir=lambda s: None).get("https://x")

    def test_main_sin_ticket_escribe_estado(self):
        with tempfile.TemporaryDirectory() as tmp:
            datos = Path(tmp) / "datos.json"
            cache = Path(tmp) / "cache.json"
            import os

            os.environ.pop("MERCADO_PUBLICO_TICKET", None)
            codigo = monitor.main(["--datos", str(datos), "--cache", str(cache)])
            self.assertEqual(codigo, 1)
            publicado = json.loads(datos.read_text())
            self.assertIn("ticket", publicado["fuentes"]["licitaciones"]["mensaje"].lower())
            self.assertEqual(publicado["items"], [])


if __name__ == "__main__":
    unittest.main()
