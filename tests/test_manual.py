import unittest
from datetime import datetime, timezone

from radar.manual import (
    PREFIJO_TITULO,
    GitHub,
    fecha_chile,
    gestionar_avisos,
    gestionar_recordatorio,
    item_desde_issue,
    leer_ingresos_manuales,
    parsear_formulario,
)
from tests.test_monitor import AHORA, ApiFalsa, crear_radar

CUERPO = """### Código de la Compra Ágil

1234-56-cot26

### Nombre

Adquisición de dron con cámara térmica

### Organismo comprador

Municipalidad de Prueba

### Región

Región de Valparaíso

### Fecha de cierre

14-11-2026 15:00

### Monto disponible (CLP)

$ 2.500.000

### Notas

_No response_
"""


def issue(numero=7, titulo=f"{PREFIJO_TITULO} Dron térmico", cuerpo=CUERPO, etiquetas=()):
    return {
        "number": numero,
        "title": titulo,
        "body": cuerpo,
        "labels": [{"name": e} for e in etiquetas],
        "html_url": f"https://github.com/o/r/issues/{numero}",
    }


class GitHubFalso(GitHub):
    def __init__(self, issues=()):
        self.llamadas = []
        self._issues = list(issues)
        super().__init__("o/r", "t", enviar=self._registrar)

    def _registrar(self, metodo, ruta, datos=None):
        self.llamadas.append((metodo, ruta, datos))
        if metodo == "GET":
            return self._issues
        if ruta == "/issues":
            return {"number": 100 + len(self.llamadas)}
        return {}


class TestFormulario(unittest.TestCase):
    def test_parsea_campos(self):
        campos = parsear_formulario(CUERPO)
        self.assertEqual(campos["codigo de la compra agil"], "1234-56-cot26")
        self.assertEqual(campos["notas"], "")

    def test_item(self):
        item = item_desde_issue(issue())
        self.assertEqual(item["id"], "CA:1234-56-COT26")
        self.assertEqual(item["origen"], "manual")
        self.assertEqual(item["monto"], 2500000)
        self.assertEqual(item["fecha_cierre"], "2026-11-14T15:00:00-03:00")
        self.assertEqual(item["organismo"], "Municipalidad de Prueba")
        self.assertIn("ficha?code=1234-56-COT26", item["url"])

    def test_sin_codigo_valido_se_omite(self):
        self.assertIsNone(item_desde_issue(issue(cuerpo="### Código de la Compra Ágil\n\nno sé")))

    def test_fechas(self):
        self.assertEqual(fecha_chile("14/11/2026"), "2026-11-14T23:59:00-03:00")
        self.assertIsNone(fecha_chile("mañana"))

    def test_solo_issues_de_ingreso(self):
        gh = GitHubFalso([issue(), issue(8, titulo="Otro tema", cuerpo="hola")])
        self.assertEqual([i["codigo"] for i in leer_ingresos_manuales(gh)], ["1234-56-COT26"])


class TestRadarConManuales(unittest.TestCase):
    def test_se_agrega_y_la_api_manda_despues(self):
        manual = item_desde_issue(issue())
        radar = crear_radar(ApiFalsa())
        radar.manuales = [manual]
        datos, _ = radar.ejecutar()
        item = datos["items"][0]
        self.assertEqual((item["origen"], item["terminos"]), ("manual", ["Dron"]))

        # Vuelve la API con la misma Compra Ágil: sus datos reemplazan a los manuales.
        from tests.test_monitor import ca_item

        api = ApiFalsa(busqueda_ca={"dron": [ca_item("1234-56-COT26", "DRON TÉRMICO (API)")]},
                       detalles_ca={"1234-56-COT26": ca_item("1234-56-COT26", "DRON TÉRMICO (API)")})
        radar2 = crear_radar(api, previos=datos)
        radar2.manuales = [manual]
        datos2, _ = radar2.ejecutar()
        item2 = datos2["items"][0]
        self.assertEqual(item2["origen"], "api")
        self.assertEqual(item2["nombre"], "DRON TÉRMICO (API)")
        self.assertEqual(item2["issue_url"], manual["issue_url"])

    def test_manual_sin_terminos_se_conserva(self):
        manual = item_desde_issue(issue(cuerpo=CUERPO.replace("Adquisición de dron con cámara térmica", "Servicio de vuelo")))
        radar = crear_radar(ApiFalsa())
        radar.manuales = [manual]
        datos, _ = radar.ejecutar()
        self.assertEqual(datos["items"][0]["terminos"], ["Ingreso manual"])

    def test_cache_conserva_claves_del_recordatorio(self):
        _, cache = crear_radar(ApiFalsa(), cache={"ultimo_recordatorio": "x", "recordatorios_abiertos": [3]}).ejecutar()
        self.assertEqual(cache["recordatorios_abiertos"], [3])


def datos_ca(ok):
    return {"fuentes": {"compra_agil": {"ok": ok, "ultima_ok": "2026-11-06T16:05:11+00:00"}}}


class TestRecordatorio(unittest.TestCase):
    # 13:30 en Chile (UTC-3 en noviembre)
    AHORA = datetime(2026, 11, 10, 16, 30, tzinfo=timezone.utc)

    def test_abre_un_recordatorio_por_bloque(self):
        gh = GitHubFalso()
        cache = gestionar_recordatorio(gh, datos_ca(False), {}, ["dron", "RPAS"], self.AHORA, "duenio")
        creados = [d for m, r, d in gh.llamadas if r == "/issues"]
        self.assertEqual(len(creados), 1)
        self.assertIn("13:00", creados[0]["title"])
        self.assertEqual(creados[0]["assignees"], ["duenio"])
        self.assertIn("- [ ] dron", creados[0]["body"])
        self.assertEqual(cache["ultimo_recordatorio"], "2026-11-10 13")

        # Misma hora otra vez: no se repite.
        gh2 = GitHubFalso()
        gestionar_recordatorio(gh2, datos_ca(False), cache, ["dron"], self.AHORA, "duenio")
        self.assertFalse([r for m, r, d in gh2.llamadas if r == "/issues"])

    def test_de_madrugada_no_avisa(self):
        gh = GitHubFalso()
        madrugada = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)  # 06:00 en Chile
        self.assertEqual(gestionar_recordatorio(gh, datos_ca(False), {}, [], madrugada, "d"), {})
        self.assertEqual(gh.llamadas, [])

    def test_cierra_cuando_la_api_vuelve(self):
        gh = GitHubFalso()
        cache = gestionar_recordatorio(gh, datos_ca(True), {"recordatorios_abiertos": [41]}, [], self.AHORA, "d")
        self.assertEqual(cache["recordatorios_abiertos"], [])
        self.assertIn(("PATCH", "/issues/41", {"state": "closed", "state_reason": "completed"}), gh.llamadas)


class TestAvisos(unittest.TestCase):
    AHORA = datetime(2026, 11, 12, 15, 0, tzinfo=timezone.utc)

    def item(self, id_, area="capacitacion", estado="abierta", detectada="2026-11-12T14:05:00+00:00", **extra):
        return {"id": id_, "codigo": id_[3:], "nombre": "Curso piloto de dron", "area": area, "estado": estado, "relevancia": "alta",
                "primera_deteccion": detectada, "fecha_cierre": "2026-11-14T15:00:00-03:00", "organismo": "Mun. X", "monto": 800000,
                "url": "https://buscador.mercadopublico.cl/ficha?code=1", **extra}

    def test_avisa_solo_lo_nuevo_de_las_areas_elegidas(self):
        datos = {"items": [
            self.item("CA:1"),
            self.item("CA:2", area="tienda"),
            self.item("CA:3", detectada="2026-11-01T00:00:00+00:00"),
            self.item("CA:4", relevancia="baja"),
        ]}
        gh = GitHubFalso()
        cache = gestionar_avisos(gh, datos, {}, ["capacitacion", "servicios"], self.AHORA, "duenio")
        creados = [d for m, r, d in gh.llamadas if r == "/issues"]
        self.assertEqual(len(creados), 1)
        self.assertIn("Capacitación", creados[0]["title"])
        self.assertIn("cierra 14-11 15:00", creados[0]["title"])
        self.assertIn("$800.000", creados[0]["body"])
        self.assertEqual(list(cache["avisos"]), ["CA:1"])
        # No se repite en la siguiente ejecución.
        gh2 = GitHubFalso()
        gestionar_avisos(gh2, datos, cache, ["capacitacion"], self.AHORA, "duenio")
        self.assertEqual(gh2.llamadas, [])

    def test_cierra_el_aviso_cuando_la_compra_cierra(self):
        gh = GitHubFalso()
        cache = gestionar_avisos(gh, {"items": [self.item("CA:1", estado="cerrada", estado_glosa="Cerrada")]}, {"avisos": {"CA:1": 55}}, ["capacitacion"], self.AHORA, "d")
        self.assertEqual(cache["avisos"], {})
        self.assertIn(("PATCH", "/issues/55", {"state": "closed", "state_reason": "completed"}), gh.llamadas)

    def test_sin_areas_no_avisa(self):
        gh = GitHubFalso()
        gestionar_avisos(gh, {"items": [self.item("CA:1")]}, {}, [], self.AHORA, "d")
        self.assertEqual(gh.llamadas, [])


if __name__ == "__main__":
    unittest.main()
