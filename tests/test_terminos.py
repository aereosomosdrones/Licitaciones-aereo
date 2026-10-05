import unittest

from radar.monitor import RUTA_TERMINOS
from radar.terminos import Diccionario, normalizar

DICC = Diccionario.desde_archivo(RUTA_TERMINOS)


class TestNormalizar(unittest.TestCase):
    def test_tildes_mayusculas_y_signos(self):
        self.assertEqual(normalizar("ADQUISICIÓN de Drones/RPAS, Fotogrametría"), "adquisicion de drones rpas fotogrametria")

    def test_vacio(self):
        self.assertEqual(normalizar(None), "")


class TestDeteccion(unittest.TestCase):
    def assertDetecta(self, texto, etiqueta):
        self.assertIn(etiqueta, DICC.buscar(texto), texto)

    def assertNoDetecta(self, texto):
        self.assertEqual(DICC.buscar(texto), [], texto)

    def test_terminos_nucleo(self):
        self.assertDetecta("ADQUISICION DE DRON PARA MUNICIPALIDAD", "Dron")
        self.assertDetecta("Compra de drones multipropósito", "Dron")
        self.assertDetecta("SERVICIO DE VUELO RPAS", "RPA / RPAS")
        self.assertDetecta("Curso operador RPA", "RPA / RPAS")
        self.assertDetecta("Adquisición UAV ala fija", "UAV / UAS")
        self.assertDetecta("Sistema VANT para vigilancia", "VANT")
        self.assertDetecta("Aeronave pilotada a distancia para monitoreo", "Aeronave no tripulada")
        self.assertDetecta("vehículo aéreo no tripulado", "Aeronave no tripulada")
        self.assertDetecta("Cuadricóptero con cámara", "Multirrotor")
        self.assertDetecta("DJI Matrice 350 RTK", "Marcas (DJI, Autel…)")
        self.assertDetecta("Obtención credencial piloto de drones DGAC", "Credencial / piloto")
        self.assertDetecta("Capacitación DAN 151", "Credencial / piloto")
        self.assertDetecta("Sistema anti-drones para recinto penitenciario", "Antidrones")

    def test_terminos_derivados(self):
        self.assertDetecta("Levantamiento aerofotogramétrico sector rural", "Fotogrametría / ortomosaico")
        self.assertDetecta("Generación de ortomosaico y curvas de nivel", "Fotogrametría / ortomosaico")
        self.assertDetecta("Levantamiento LiDAR cuenca", "LiDAR aéreo")
        self.assertDetecta("Servicio de filmación aérea aniversario comunal", "Servicios aéreos")
        self.assertDetecta("Inspección con dron de techumbres", "Servicios aéreos")

    def test_falsos_positivos(self):
        self.assertNoDetecta("Licencias UiPath RPA para automatización robótica de procesos")
        self.assertNoDetecta("Implementación RPA robotic process automation")
        self.assertNoDetecta("Proyecto piloto de reciclaje")
        self.assertNoDetecta("Plan piloto de atención primaria")
        self.assertNoDetecta("Escáner automotriz Autel MaxiSys")
        self.assertNoDetecta("Cámara DJI Osmo Pocket 3")
        self.assertNoDetecta("Servicio de aseo y ornato")
        self.assertNoDetecta("Adquisición de pasajes aéreos")

    def test_rpa_excluido_no_anula_otros_terminos(self):
        # La exclusión de "automatización robótica" solo afecta al término RPA.
        self.assertEqual(DICC.buscar("RPA automatización robótica de procesos y dron"), ["Dron"])

    def test_categorias(self):
        self.assertEqual(DICC.categorias(["Dron", "LiDAR aéreo"]), ["derivado", "nucleo"])


if __name__ == "__main__":
    unittest.main()
