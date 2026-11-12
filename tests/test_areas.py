import unittest

from radar.areas import AREAS, clasificar


class TestAreas(unittest.TestCase):
    def assertArea(self, area, nombre, **extra):
        self.assertEqual(clasificar({"nombre": nombre, **extra}), area, nombre)

    def test_capacitacion(self):
        for nombre in [
            "CURSO RENOVACION CREDENCIAL DE PILOTOS RPAS",
            "ADQUISICION DE CURSO DE OPERADOR DE DRONES (RPAS) Y VUELO NOCTURNO",
            "Servicio de capacitación en operación y manejo de drones para Seguridad Pública.",
            "CURSO DE OPERACION E INSPECCION OBRAS CON DRONES",
            "CONTRATACIÓN ACTIVIDAD DE CAPACITACIÓN “PILOTO DE RPA CON ESPECIALIZACIÓN EN RTK Y FOTOGRAMETRÍA”",
            "ADQ DE 6 CUOTAS DE INSCRIPCIÓN DE CURSO. SECCIÓN PERSONAL",
            "Servicio de certificación operador de RPAS ante la DGAC.",
        ]:
            self.assertArea("capacitacion", nombre)

    def test_servicios(self):
        for nombre in [
            "Servicio Levantamiento Aerofotogramétrico mediante RPAS de Quebradas",
            "SERVICIO DE FILMACION CON DRONE",
            "Servicio de aplicación agrícola con drones, capacitación y validación de cobertura",
            "CONTRATACION DRON CON PILOTO. SOL. 246/ADM.",
            "ADQUISICIÓN DE SERVICIO DE DRON PARA OBTENER INFORMACIÓN PARA META PACCC",
            "“SHOW DE DRONES 2027”",
        ]:
            self.assertArea("servicios", nombre)

    def test_asesorias(self):
        self.assertArea("certifica", "Servicio de asesoría para acreditación operador RPAS (Certificación DGAC)")
        self.assertArea("gestion", "SERVICIO DE REPARACIÓN Y MANTENCIÓN DE AERONAVES (DRONES) RPAS")
        self.assertArea("gestion", "ADQUISICIÓN DEL SERVICIO DE MANTENCIÓN PREVENTIVA DE DRON")

    def test_tienda(self):
        for nombre in [
            "ADQUISICIÓN DE DRONE MÁS CONTROL RC",
            "Adquisición y Habilitación de Dron",
            "Drone, Accesorios y Capacitación, ULS 2495",
            "N° 8372 Dron para Programa MAIS",
            "Adquisición de un 01 Dron con cámara térmica para la Municipalidad de San Pedro de Atacama.",
            "BATERIAS PARA RPAS MATRICE 300 TB65 S.A. 064",
            "Adquisición de Escáner LiDAR 3D SLAM",
        ]:
            self.assertArea("tienda", nombre)

    def test_seguros_quedan_fuera_del_ecosistema(self):
        self.assertArea("otros", "Seguro para Dron")
        self.assertArea("otros", "176.-ADQUISICIÓN DE PÓLIZA DE SEGURO PARA DRONES MARCA DJI")

    def test_titulo_que_no_dice_nada_usa_el_detalle(self):
        self.assertArea("capacitacion", "DEPARTAMENTO DE GEOLOGIA", productos=["Capacitación — Curso piloto de dron para 4 funcionarios"])

    def test_todas_las_areas_estan_definidas(self):
        self.assertEqual(set(AREAS), {"capacitacion", "servicios", "certifica", "gestion", "tienda", "otros"})


if __name__ == "__main__":
    unittest.main()
