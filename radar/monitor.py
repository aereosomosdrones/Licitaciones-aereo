"""Radar de licitaciones y Compras Ágiles de drones para Aéreo.

Uso:
    MERCADO_PUBLICO_TICKET=xxxx python -m radar.monitor

Cada ejecución:
  1. Licitaciones: descarga el listado de licitaciones activas, revisa el
     nombre de todas y el detalle (descripción + ítems) de las que aún no ha
     revisado, y refresca el estado de las que ya sigue.
  2. Compra Ágil: busca cada término de config/terminos.json con el buscador
     de la API y verifica cada resultado contra los patrones.
  3. Escribe docs/data/licitaciones.json, que lee el dashboard.

Está pensado para correr cada hora (GitHub Actions, cron o similar).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .areas import clasificar
from .fuentes import (
    ESTADOS_FINALES,
    ApiCompraAgil,
    ApiLicitaciones,
    ClienteHTTP,
    CuotaAgotada,
    ErrorAPI,
    ahora_utc,
    compra_agil_desde_detalle,
    compra_agil_desde_listado,
    fecha_filtro_compra_agil,
    licitacion_desde_detalle,
    licitacion_desde_resumen,
)
from .terminos import Diccionario, normalizar

log = logging.getLogger("radar")

RAIZ = Path(__file__).resolve().parent.parent
RUTA_TERMINOS = RAIZ / "config" / "terminos.json"
RUTA_DATOS = RAIZ / "docs" / "data" / "licitaciones.json"
RUTA_CACHE = RAIZ / "data" / "cache.json"

INTERVALO_MINUTOS = 60
CAMPOS_TEXTO = ("nombre", "descripcion")


class Config:
    # Licitaciones
    max_detalles_licitaciones = 120  # detalles de licitaciones nuevas por ejecución
    refrescar_seguidas_horas = 6  # cada cuánto se refresca el detalle de las ya detectadas
    # Compra Ágil
    dias_ventana_compra_agil = 45
    max_paginas_compra_agil = 8  # páginas de 10 resultados por término
    barrido_completo_horas = 24  # una vez al día se repasan los 45 días completos
    horas_ventana_recientes = 72  # cada hora se busca lo publicado en estas últimas horas
    max_detalles_compra_agil = 120
    max_refrescos_compra_agil = 40  # compras cerradas a las que se les consulta el resultado, por ejecución
    min_refrescos_compra_agil = 15
    presupuesto_refresco_segundos = 4 * 60
    refrescar_cerradas_horas = 8
    presupuesto_busquedas_segundos = 15 * 60
    max_busquedas_fallidas_seguidas = 3  # sin ninguna exitosa, se da la API por caída
    # General
    presupuesto_segundos = 10 * 60  # tiempo máximo pidiendo detalles, por fuente y ejecución
    dias_retencion = 180  # se olvidan procesos cerrados hace más de esto


_CAPACITACION = re.compile(r"capacitaci|\bcursos?\b|\btaller(es)?\b|certificacion|cuotas? de inscripcion")
_REMITE_A_ADJUNTO = re.compile(
    r"adjunt|anexo|terminos de referencia|\btdr\b|\bbases\b|especificaciones tecnicas|requerimientos? tecnicos?|segun (archivo|documento|detalle)"
)
_PALABRAS_SIN_TEMA = re.compile(
    r"\b(capacitacion(es)?|cursos?|taller(es)?|servicios?|adquisicion|adq|compra|contratacion|solicitud|sol|segun|archivo|documento"
    r"|adjuntos?|anexos?|detalles?|caracteristicas|especificaciones|tecnicas?|terminos|referencia|bases|favor|revisar|ver|se|requiere"
    r"|para|por|con|del?|la|las|el|los|en|un|una|y|o|a|al|funcionari[oa]s?|personal|municipal(es)?|departamento|direccion|unidad"
    r"|programa|oficina|seccion|cuotas?|inscripcion|empleados|n|no|nro|numero|cdp|sc|oc|id)\b|\d+"
)


def es_capacitacion_sin_tema(item: dict) -> bool:
    """Capacitación cuyo texto público no dice de qué trata y remite a un adjunto
    ("CAPACITACIÓN SEGÚN ARCHIVO ADJUNTO"). El radar no puede leer el adjunto: queda
    para revisión de una persona."""
    texto = normalizar(" | ".join([item.get("nombre") or "", item.get("descripcion") or "", *(item.get("productos") or [])]))
    if not _CAPACITACION.search(texto) or not _REMITE_A_ADJUNTO.search(texto):
        return False
    tema = _PALABRAS_SIN_TEMA.sub(" ", texto.replace("|", " ")).split()
    return len({p for p in tema if len(p) > 3}) <= 4


def _iso(fecha: datetime) -> str:
    return fecha.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse(valor: str | None) -> datetime | None:
    if not valor:
        return None
    try:
        fecha = datetime.fromisoformat(valor.replace("Z", "+00:00"))
    except ValueError:
        return None
    return fecha if fecha.tzinfo else fecha.replace(tzinfo=timezone.utc)


def _leer_json(ruta: Path, defecto: dict) -> dict:
    try:
        return json.loads(ruta.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return defecto


def _escribir_json(ruta: Path, datos: dict) -> None:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    temporal = ruta.with_suffix(".tmp")
    temporal.write_text(json.dumps(datos, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    temporal.replace(ruta)


class Radar:
    def __init__(
        self,
        diccionario: Diccionario,
        api_licitaciones: ApiLicitaciones | None,
        api_compra_agil: ApiCompraAgil | None,
        datos_previos: dict,
        cache: dict,
        ahora: datetime,
        config: Config | None = None,
        reloj=time.monotonic,
        manuales: list[dict] | None = None,
    ):
        self.dicc = diccionario
        self.manuales = manuales or []
        self.lic = api_licitaciones
        self.ca = api_compra_agil
        self.ahora = ahora
        self.cfg = config or Config()
        self.reloj = reloj
        self.inicio = reloj()
        self.items: dict[str, dict] = {i["id"]: i for i in datos_previos.get("items", [])}
        self.cache = {
            **cache,  # conserva otras claves (p. ej. las del recordatorio manual)
            # Códigos de licitaciones activas cuyo detalle ya se revisó (lista ordenada en disco).
            "lic_revisadas": set(cache.get("lic_revisadas", [])),
            "ca_descartadas": cache.get("ca_descartadas", {}),
            "ca_pendientes": cache.get("ca_pendientes", {}),  # candidatas de la segunda pasada sin revisar
            "ca_por_revisar": cache.get("ca_por_revisar", {}),  # capacitaciones cuyo tema solo está en el adjunto
            "ca_ultimo_barrido_completo": cache.get("ca_ultimo_barrido_completo"),
            "ca_ultima_ejecucion_ok": cache.get("ca_ultima_ejecucion_ok"),
        }
        self.fuentes_previas = datos_previos.get("fuentes", {})
        self.nuevas: list[str] = []
        self.errores_detalle = 0
        self.limitada = False  # la API respondió 429: no pedir más detalles en esta fuente
        self.advertencias: list[str] = []
        self.sin_ticket = api_licitaciones is None and api_compra_agil is None

    # ------------------------------------------------------------ utilidades

    def _con_tiempo(self) -> bool:
        if self.limitada:
            return False
        return self.reloj() - self.inicio < self.cfg.presupuesto_segundos

    def _terminos(self, item: dict) -> list[str]:
        return self.dicc.buscar(
            *(item.get(c) for c in CAMPOS_TEXTO), *(item.get("productos") or []), *(item.get("adjuntos") or [])
        )

    def _relevancia(self, item: dict) -> tuple[str, str]:
        """('alta'|'baja', dónde coincide). Alta: el título habla de drones, o lo hace lo que
        se pide (productos, adjuntos). Baja: solo una mención al pasar en un texto largo."""
        if item.get("origen") == "manual":
            return "alta", "manual"
        if self.dicc.buscar(item.get("nombre")):
            return "alta", "titulo"
        productos = item.get("productos") or []
        lineas = [*productos, *(item.get("adjuntos") or [])]
        con_termino = [linea for linea in lineas if self.dicc.buscar(linea)]
        if con_termino and (len(productos) <= 3 or 2 * len(con_termino) >= len(lineas)):
            return "alta", "detalle"
        return "baja", "mencion"

    def _guardar(self, nuevo: dict, terminos: list[str], detalle: bool) -> None:
        """Fusiona el proceso con lo ya conocido conservando la fecha de detección."""
        ahora = _iso(self.ahora)
        previo = self.items.get(nuevo["id"])
        item = {**(previo or {}), **{k: v for k, v in nuevo.items() if v not in (None, "", [])}}
        item["terminos"] = terminos
        item["categorias"] = self.dicc.categorias(terminos)
        item["ultima_actualizacion"] = ahora
        if detalle:
            item["detalle_actualizado"] = ahora
        if previo is None:
            item["primera_deteccion"] = ahora
            self.nuevas.append(item["id"])
        elif previo.get("estado") != item.get("estado"):
            item["cambio_estado"] = ahora
        if item.get("descripcion") and len(item["descripcion"]) > 2000:
            item["descripcion"] = item["descripcion"][:2000] + "…"
        self.items[item["id"]] = item

    def _detalle(self, api, codigo: str) -> dict | None:
        """Un detalle que falla no debe detener la revisión del resto."""
        try:
            return api.detalle(codigo)
        except CuotaAgotada as e:
            # Se conserva lo avanzado; lo pendiente se revisa en la próxima ejecución.
            self.limitada = True
            self.advertencias.append(str(e))
            log.warning("%s", e)
            return None
        except ErrorAPI as e:
            self.errores_detalle += 1
            log.warning("No se pudo obtener el detalle de %s: %s", codigo, e)
            return None

    def _necesita_refresco(self, item: dict | None, horas: float) -> bool:
        if not item or not item.get("detalle_actualizado"):
            return True
        ultima = _parse(item["detalle_actualizado"])
        return ultima is None or self.ahora - ultima > timedelta(hours=horas)

    # ---------------------------------------------------------- licitaciones

    def revisar_licitaciones(self) -> dict:
        activas = self.lic.activas()
        log.info("Licitaciones activas: %s", len(activas))
        codigos_activos = set()
        detalles = 0
        pendientes_detalle = 0
        revisadas = self.cache["lic_revisadas"]
        # Primero las que ya coinciden por nombre, para que su detalle llegue antes.
        activas = sorted(activas, key=lambda r: not self.dicc.buscar(r.get("Nombre")))

        for resumen in activas:
            codigo = resumen.get("CodigoExterno")
            if not codigo:
                continue
            codigos_activos.add(codigo)
            base = licitacion_desde_resumen(resumen)
            previo = self.items.get(base["id"])
            terminos_nombre = self.dicc.buscar(base["nombre"])

            # El detalle se pide para: toda licitación nunca revisada (para buscar
            # en descripción e ítems) y las ya detectadas cada N horas.
            ya_revisada = codigo in revisadas
            pedir_detalle = (not ya_revisada) or (
                previo is not None and self._necesita_refresco(previo, self.cfg.refrescar_seguidas_horas)
            )
            if pedir_detalle and detalles < self.cfg.max_detalles_licitaciones and self._con_tiempo():
                detalle = self._detalle(self.lic, codigo)
                detalles += 1
                if detalle:
                    revisadas.add(codigo)
                    item = licitacion_desde_detalle(detalle)
                    terminos = self._terminos(item)
                    if terminos:
                        self._guardar(item, terminos, detalle=True)
                    elif previo:
                        # El detalle ya no menciona drones (p. ej. fue corregido): se deja de seguir.
                        self.items.pop(base["id"], None)
                    continue
            elif pedir_detalle:
                pendientes_detalle += 1

            if terminos_nombre or previo:
                self._guardar(base, sorted(set(terminos_nombre) | set((previo or {}).get("terminos", []))), detalle=False)

        # Licitaciones seguidas que ya no están activas: refrescar su estado
        # (cerrada → adjudicada/desierta) hasta que lleguen a un estado final.
        for item in list(self.items.values()):
            if item["fuente"] != "licitacion" or item["codigo"] in codigos_activos:
                continue
            if item.get("estado") in ESTADOS_FINALES:
                continue
            if not self._necesita_refresco(item, self.cfg.refrescar_seguidas_horas) or not self._con_tiempo():
                continue
            detalle = self._detalle(self.lic, item["codigo"])
            detalles += 1
            if detalle:
                actualizado = licitacion_desde_detalle(detalle)
                self._guardar(actualizado, self._terminos(actualizado) or item["terminos"], detalle=True)

        # Olvidar las revisadas que ya no están activas para que la caché no crezca sin fin.
        self.cache["lic_revisadas"] = revisadas & codigos_activos
        return {
            "revisadas": len(activas),
            "detalles_consultados": detalles,
            "pendientes_por_revisar": pendientes_detalle,
        }

    # ----------------------------------------------------------- compra ágil

    def _buscar_ventana(self, consulta: str, campo: str, desde: datetime, hasta: datetime, nivel: int = 0) -> list[dict]:
        """Busca en [desde, hasta]. Si la API no alcanza a responder (504) o hay más
        resultados de los que se leen, divide la ventana de fechas en dos y repite."""
        z = fecha_filtro_compra_agil
        divisible = hasta - desde > timedelta(hours=12) and nivel < 6
        if self.reloj() - self._inicio_busquedas > self.cfg.presupuesto_busquedas_segundos:
            raise ErrorAPI("sin tiempo para completar la búsqueda")
        try:
            items, truncado = self.ca.buscar(
                consulta,
                max_paginas=self.cfg.max_paginas_compra_agil,
                **{f"{campo}_desde": z(desde), f"{campo}_hasta": z(hasta)},
            )
        except CuotaAgotada:
            raise
        except ErrorAPI:
            # Ante errores se divide poco (hasta 8 tramos): si la API está caída no
            # tiene sentido multiplicar los intentos.
            if not divisible or nivel >= 3:
                raise
            items, truncado = [], True
        if truncado and divisible:
            medio = desde + (hasta - desde) / 2
            log.info("Compra Ágil: '%s' dividiendo %s → %s", consulta, z(desde)[:10], z(hasta)[:10])
            return self._buscar_ventana(consulta, campo, desde, medio, nivel + 1) + self._buscar_ventana(
                consulta, campo, medio, hasta, nivel + 1
            )
        return items

    def _barrer(self, consultas: list[str], desde: datetime) -> tuple[dict[str, dict], list[str]]:
        """Busca cada consulta entre lo publicado desde `desde`. Devuelve {código: fila} y las consultas que fallaron."""
        encontrados: dict[str, dict] = {}
        fallidas: list[str] = []
        exitosas = 0
        for n, consulta in enumerate(consultas):
            if self.limitada:
                fallidas.extend(consultas[n:])
                break
            if not exitosas and len(fallidas) >= self.cfg.max_busquedas_fallidas_seguidas:
                # Si las primeras búsquedas fallan todas, la API está caída: no tiene
                # sentido gastar el resto de la hora insistiendo.
                log.warning("Compra Ágil: la API no responde; se reintenta en la próxima ejecución")
                fallidas.extend(consultas[n:])
                break
            if self.reloj() - self._inicio_busquedas > self.cfg.presupuesto_busquedas_segundos:
                log.warning("Compra Ágil: sin tiempo para las búsquedas restantes")
                fallidas.extend(consultas[n:])
                break
            t0 = self.reloj()
            try:
                resultados = self._buscar_ventana(consulta, "publicado", desde, self.ahora + timedelta(hours=1))
                log.info("Compra Ágil: '%s' → %s resultados (%.0f s)", consulta, len(resultados), self.reloj() - t0)
                exitosas += 1
            except CuotaAgotada:
                fallidas.extend(consultas[n:])
                self.limitada = True
                break
            except ErrorAPI as e:
                log.warning("Compra Ágil: búsqueda '%s' falló: %s", consulta, e)
                fallidas.append(consulta)
                continue
            for bruto in resultados:
                if bruto.get("codigo"):
                    encontrados[bruto["codigo"]] = bruto
        return encontrados, fallidas

    def revisar_compra_agil(self) -> dict:
        """Tres pasos por ejecución:

        1. Consultas precisas (dron, RPAS, DJI…) sobre lo publicado en las últimas 72 h
           (una vez al día, sobre los 45 días completos).
        2. Segunda pasada: consultas amplias de capacitación (curso, capacitación, DGAC…).
           Sus resultados solo se guardan si el detalle (productos, adjuntos) habla de
           drones; así aparecen las compras cuyo título no lo dice.
        3. Resultado de las compras ya cerradas: quién cotizó, quién ganó y si AÉREO participó.
        """
        cfg = self.cfg
        ultimo_completo = _parse(self.cache.get("ca_ultimo_barrido_completo"))
        ultima_ok = _parse(self.cache.get("ca_ultima_ejecucion_ok"))
        completo = (
            ultimo_completo is None
            or ultima_ok is None
            or self.ahora - ultimo_completo > timedelta(hours=cfg.barrido_completo_horas)
        )
        recientes = timedelta(hours=cfg.horas_ventana_recientes)
        log.info("Compra Ágil: barrido %s", "completo" if completo else "de lo publicado recién")

        # 1. Consultas precisas ------------------------------------------------
        self._inicio_busquedas = self.reloj()
        precisas = self.dicc.consultas_compra_agil
        desde = self.ahora - (timedelta(days=cfg.dias_ventana_compra_agil) if completo else recientes)
        encontrados, fallidas = self._barrer(precisas, desde)
        if fallidas and len(fallidas) == len(precisas):
            raise ErrorAPI("Ninguna búsqueda de Compra Ágil respondió (la API puede estar caída o limitando consultas).")
        if fallidas:
            self.advertencias.append(f"{len(fallidas)} de {len(precisas)} búsquedas no respondieron: {', '.join(fallidas[:6])}")

        # 2. Segunda pasada: candidatas de capacitación --------------------------
        descartadas = self.cache["ca_descartadas"]
        pendientes = self.cache["ca_pendientes"]
        amplias = self.dicc.consultas_amplias
        ultima_amplia = _parse(self.cache.get("ca_ultima_amplia"))
        if amplias:
            if completo or ultima_amplia is None:
                desde_amplia = self.ahora - recientes
            else:
                desde_amplia = max(ultima_amplia - timedelta(hours=3), self.ahora - recientes)
            self._inicio_busquedas = self.reloj()
            candidatas, fallidas_amplias = self._barrer(amplias, desde_amplia)
            for codigo, fila in candidatas.items():
                if codigo in encontrados or f"CA:{codigo}" in self.items or codigo in descartadas or codigo in pendientes:
                    continue
                if self.dicc.buscar(fila.get("nombre")):
                    encontrados[codigo] = fila  # el título ya habla de drones
                else:
                    cierre = compra_agil_desde_listado(fila).get("fecha_cierre")
                    pendientes[codigo] = {"cierre": cierre, "visto": _iso(self.ahora)}
            if fallidas_amplias:
                self.advertencias.append(f"Segunda pasada incompleta: {', '.join(fallidas_amplias[:6])}")
            else:
                self.cache["ca_ultima_amplia"] = _iso(self.ahora)

        # Detalle de lo encontrado por las consultas precisas ---------------------
        detalles = 0
        self.inicio = self.reloj()  # el presupuesto de detalles empieza después de las búsquedas
        for codigo, bruto in encontrados.items():
            base = compra_agil_desde_listado(bruto)
            previo = self.items.get(base["id"])
            cambio = base.get("fecha_ultimo_cambio") or ""
            terminos = self.dicc.buscar(base["nombre"])

            if not terminos and not previo and descartadas.get(codigo, {}).get("cambio") == cambio:
                continue  # ya se revisó su detalle y no trata de drones

            cambio_previo = (previo or {}).get("fecha_ultimo_cambio")
            pedir_detalle = previo is None or cambio_previo != cambio or not previo.get("detalle_actualizado")
            if pedir_detalle and detalles < cfg.max_detalles_compra_agil and self._con_tiempo():
                detalle = self._detalle(self.ca, codigo)
                detalles += 1
                if detalle:
                    item = compra_agil_desde_detalle(detalle)
                    terminos = sorted(set(terminos) | set(self._terminos(item)))
                    if terminos:
                        descartadas.pop(codigo, None)
                        self._guardar(item, terminos, detalle=True)
                    else:
                        descartadas[codigo] = {"cambio": cambio, "visto": _iso(self.ahora)}
                    continue

            if terminos or previo:
                terminos = sorted(set(terminos) | set((previo or {}).get("terminos", [])))
                self._guardar(base, terminos, detalle=False)

        # Detalle de las candidatas de la segunda pasada (primero las que cierran antes) --
        segunda_pasada = 0
        for codigo in sorted(pendientes, key=lambda c: pendientes[c].get("cierre") or "9"):
            cierre = _parse(pendientes[codigo].get("cierre"))
            if cierre and cierre < self.ahora:
                del pendientes[codigo]  # ya cerró: no sirve para cotizar
                continue
            if detalles >= cfg.max_detalles_compra_agil or not self._con_tiempo():
                break
            detalle = self._detalle(self.ca, codigo)
            detalles += 1
            if not detalle:
                continue
            del pendientes[codigo]
            item = compra_agil_desde_detalle(detalle)
            terminos = self._terminos(item)
            if terminos:
                item["via"] = "segunda pasada"
                self._guardar(item, terminos, detalle=True)
                segunda_pasada += 1
                log.info("Segunda pasada: %s | %s", codigo, item["nombre"][:80])
            else:
                descartadas[codigo] = {"visto": _iso(self.ahora)}
                if es_capacitacion_sin_tema(item):
                    self.cache["ca_por_revisar"][codigo] = {
                        k: item.get(k)
                        for k in ("id", "codigo", "nombre", "organismo", "region", "fecha_publicacion", "fecha_cierre", "monto", "url", "adjuntos")
                    }

        detalles += self._refrescar_compra_agil(cfg.max_detalles_compra_agil - detalles)

        limite = self.ahora - timedelta(days=cfg.dias_ventana_compra_agil + 15)
        self.cache["ca_descartadas"] = {
            c: v for c, v in descartadas.items() if (_parse(v.get("visto")) or self.ahora) > limite
        }
        self.cache["ca_por_revisar"] = {
            c: v for c, v in self.cache["ca_por_revisar"].items() if (_parse(v.get("fecha_cierre")) or self.ahora) > self.ahora
        }
        resumen = {
            "revisadas": len(encontrados),
            "detalles_consultados": detalles,
            "segunda_pasada": segunda_pasada,
            "pendientes_segunda_pasada": len(pendientes),
            "modo": "completo" if completo else "recientes",
        }
        if fallidas:
            # No se da por buena la ejecución: lo que no respondió se vuelve a pedir completo.
            return resumen
        self.cache["ca_ultima_ejecucion_ok"] = _iso(self.ahora)
        if completo:
            self.cache["ca_ultimo_barrido_completo"] = _iso(self.ahora)
        return resumen

    def _refrescar_compra_agil(self, cupo: int) -> int:
        """Consulta el detalle de las compras que ya cerraron hasta conocer su resultado
        (quién cotizó, quién ganó). Mercado Público lo publica al seleccionar proveedor."""
        por_refrescar = []
        for item in self.items.values():
            if item.get("fuente") != "compra_agil" or item.get("origen") == "manual":
                continue
            estado = item.get("estado")
            cierre = _parse(item.get("fecha_cierre"))
            if estado in ("desierta", "cancelada") or (estado == "adjudicada" and "resultado" in item):
                continue
            if estado == "abierta" and (cierre is None or cierre > self.ahora):
                continue
            if self._necesita_refresco(item, self.cfg.refrescar_cerradas_horas):
                por_refrescar.append(item)
        por_refrescar.sort(key=lambda i: i.get("fecha_cierre") or "", reverse=True)
        hechos = 0
        # Tiempo propio: que una segunda pasada larga no deje sin resultados a las cerradas.
        limite = self.reloj() + self.cfg.presupuesto_refresco_segundos
        for item in por_refrescar[: max(self.cfg.min_refrescos_compra_agil, min(cupo, self.cfg.max_refrescos_compra_agil))]:
            if self.limitada or self.reloj() > limite:
                break
            detalle = self._detalle(self.ca, item["codigo"])
            hechos += 1
            if not detalle:
                continue
            actualizado = compra_agil_desde_detalle(detalle)
            self._guardar(actualizado, self._terminos(actualizado) or item.get("terminos", []), detalle=True)
            if actualizado.get("estado") == "adjudicada":
                self.items[item["id"]].setdefault("resultado", {})  # sin ofertas publicadas: no insistir
        return hechos

    # ------------------------------------------------------------- ejecución

    def _ejecutar_fuente(self, nombre: str, api, funcion) -> dict:
        previo = self.fuentes_previas.get(nombre, {})
        if api is None:
            if self.sin_ticket:
                return {**previo, "ok": False, "mensaje": "Falta el ticket de Mercado Público (MERCADO_PUBLICO_TICKET)."}
            return previo or {"ok": False, "mensaje": "No revisada aún."}
        self.errores_detalle = 0
        self.limitada = False
        self.advertencias = []
        self.inicio = self.reloj()
        try:
            resumen = funcion()
            return {
                "ok": True,
                "mensaje": "OK",
                "ultima_ok": _iso(self.ahora),
                "errores_detalle": self.errores_detalle,
                "advertencias": self.advertencias,
                **resumen,
            }
        except CuotaAgotada as e:
            log.error("%s: %s", nombre, e)
            return {"ok": False, "mensaje": str(e), "ultima_ok": previo.get("ultima_ok")}
        except (ErrorAPI, OSError) as e:
            log.error("%s: %s", nombre, e)
            return {"ok": False, "mensaje": str(e)[:300], "ultima_ok": previo.get("ultima_ok")}

    def _agregar_manuales(self) -> None:
        """Compras Ágiles ingresadas a mano (respaldo cuando la API no responde)."""
        for manual in self.manuales:
            previo = self.items.get(manual["id"])
            if previo and previo.get("origen") != "manual":
                previo["issue_url"] = manual.get("issue_url")  # la API ya la trajo: manda la API
                continue
            self._guardar(manual, self._terminos(manual) or ["Ingreso manual"], detalle=False)

    def _reevaluar(self) -> None:
        """Aplica los términos vigentes a todo lo guardado: si se corrige config/terminos.json,
        los procesos que ya no coinciden desaparecen y las etiquetas se actualizan."""
        for id_, item in list(self.items.items()):
            terminos = self._terminos(item)
            if not terminos and item.get("origen") == "manual":
                terminos = ["Ingreso manual"]  # la eligió una persona: se conserva
            if terminos:
                item["area"] = clasificar(item)
                item["relevancia"], item["coincide_en"] = self._relevancia(item)
            if not terminos:
                log.info("Ya no coincide con los términos: %s %s", item.get("codigo"), (item.get("nombre") or "")[:80])
                del self.items[id_]
                self.nuevas = [n for n in self.nuevas if n != id_]
            else:
                item["terminos"] = terminos
                item["categorias"] = self.dicc.categorias(terminos)

    def _cierre_por_fecha(self) -> None:
        for item in self.items.values():
            cierre = _parse(item.get("fecha_cierre"))
            if item.get("estado") == "abierta" and cierre and cierre < self.ahora:
                item["estado"] = "cerrada"
                item["estado_glosa"] = "Cerrada (plazo vencido)"

    def _podar(self) -> None:
        limite = self.ahora - timedelta(days=self.cfg.dias_retencion)
        for id_, item in list(self.items.items()):
            referencia = _parse(item.get("fecha_cierre")) or _parse(item.get("primera_deteccion"))
            if referencia and referencia < limite:
                del self.items[id_]

    def ejecutar(self) -> tuple[dict, dict]:
        fuentes = {
            "licitaciones": self._ejecutar_fuente("licitaciones", self.lic, self.revisar_licitaciones),
            "compra_agil": self._ejecutar_fuente("compra_agil", self.ca, self.revisar_compra_agil),
        }
        self._agregar_manuales()
        self._reevaluar()
        self._cierre_por_fecha()
        self._podar()
        items = sorted(
            self.items.values(),
            key=lambda i: (i.get("estado") != "abierta", i.get("fecha_cierre") or "9999"),
        )
        datos = {
            "generado": _iso(self.ahora),
            "proxima_actualizacion": _iso(self.ahora + timedelta(minutes=INTERVALO_MINUTOS)),
            "intervalo_minutos": INTERVALO_MINUTOS,
            "fuentes": fuentes,
            "terminos": [{"etiqueta": t.etiqueta, "categoria": t.categoria} for t in self.dicc.terminos],
            "nuevas_en_esta_ejecucion": self.nuevas,
            "por_revisar": sorted(self.cache["ca_por_revisar"].values(), key=lambda i: i.get("fecha_cierre") or "9"),
            "items": items,
        }
        return datos, {**self.cache, "lic_revisadas": sorted(self.cache["lic_revisadas"])}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Radar de licitaciones de drones en Mercado Público")
    parser.add_argument("--solo", choices=["licitaciones", "compra_agil"], help="Revisar solo una fuente")
    parser.add_argument("--datos", type=Path, default=RUTA_DATOS)
    parser.add_argument("--cache", type=Path, default=RUTA_CACHE)
    parser.add_argument("--terminos", type=Path, default=RUTA_TERMINOS)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ticket = os.environ.get("MERCADO_PUBLICO_TICKET", "").strip()
    if not ticket:
        log.error("Falta la variable de entorno MERCADO_PUBLICO_TICKET (pídelo en https://www.chilecompra.cl/api/).")

    # La API de Licitaciones limita las consultas seguidas (HTTP 429): se espacian más.
    http = ClienteHTTP(pausa=2.0)
    api_lic = ApiLicitaciones(ticket, http) if ticket and args.solo != "compra_agil" else None
    api_ca = ApiCompraAgil(ticket, ClienteHTTP(reintentos=1, pausa=1.2)) if ticket and args.solo != "licitaciones" else None

    from .manual import github_desde_entorno, leer_ingresos_manuales

    manuales = leer_ingresos_manuales(github_desde_entorno())
    if manuales:
        log.info("Compras Ágiles ingresadas a mano: %s", len(manuales))
    radar = Radar(
        Diccionario.desde_archivo(args.terminos),
        api_lic,
        api_ca,
        _leer_json(args.datos, {}),
        _leer_json(args.cache, {}),
        ahora_utc(),
        manuales=manuales,
    )
    datos, cache = radar.ejecutar()
    _escribir_json(args.datos, datos)
    _escribir_json(args.cache, cache)

    abiertas = sum(1 for i in datos["items"] if i.get("estado") == "abierta")
    log.info("Procesos seguidos: %s (%s abiertos, %s nuevos)", len(datos["items"]), abiertas, len(radar.nuevas))
    for id_ in radar.nuevas:
        item = radar.items[id_]
        log.info("NUEVO %s | %s | %s", item["codigo"], item["nombre"][:90], ", ".join(item["terminos"]))
    if not ticket or not any(f["ok"] for f in datos["fuentes"].values()):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
