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
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
    licitacion_desde_detalle,
    licitacion_desde_resumen,
)
from .terminos import Diccionario

log = logging.getLogger("radar")

RAIZ = Path(__file__).resolve().parent.parent
RUTA_TERMINOS = RAIZ / "config" / "terminos.json"
RUTA_DATOS = RAIZ / "docs" / "data" / "licitaciones.json"
RUTA_CACHE = RAIZ / "data" / "cache.json"

INTERVALO_MINUTOS = 60
CAMPOS_TEXTO = ("nombre", "descripcion")


class Config:
    # Licitaciones
    max_detalles_licitaciones = 250  # detalles de licitaciones nuevas por ejecución
    refrescar_seguidas_horas = 6  # cada cuánto se refresca el detalle de las ya detectadas
    # Compra Ágil
    dias_ventana_compra_agil = 45
    max_paginas_compra_agil = 5
    barrido_completo_horas = 12  # entre barridos completos solo se piden cambios recientes
    max_detalles_compra_agil = 120
    # General
    presupuesto_segundos = 25 * 60  # tiempo máximo para pedir detalles en una ejecución
    dias_retencion = 180  # se olvidan procesos cerrados hace más de esto


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
    ):
        self.dicc = diccionario
        self.lic = api_licitaciones
        self.ca = api_compra_agil
        self.ahora = ahora
        self.cfg = config or Config()
        self.reloj = reloj
        self.inicio = reloj()
        self.items: dict[str, dict] = {i["id"]: i for i in datos_previos.get("items", [])}
        self.cache = {
            # Códigos de licitaciones activas cuyo detalle ya se revisó (lista ordenada en disco).
            "lic_revisadas": set(cache.get("lic_revisadas", [])),
            "ca_descartadas": cache.get("ca_descartadas", {}),
            "ca_ultimo_barrido_completo": cache.get("ca_ultimo_barrido_completo"),
            "ca_ultima_ejecucion_ok": cache.get("ca_ultima_ejecucion_ok"),
        }
        self.fuentes_previas = datos_previos.get("fuentes", {})
        self.nuevas: list[str] = []
        self.errores_detalle = 0
        self.sin_ticket = api_licitaciones is None and api_compra_agil is None

    # ------------------------------------------------------------ utilidades

    def _con_tiempo(self) -> bool:
        return self.reloj() - self.inicio < self.cfg.presupuesto_segundos

    def _terminos(self, item: dict) -> list[str]:
        return self.dicc.buscar(*(item.get(c) for c in CAMPOS_TEXTO), *(item.get("productos") or []))

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
        except CuotaAgotada:
            raise
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

    def revisar_compra_agil(self) -> dict:
        ultimo_completo = _parse(self.cache.get("ca_ultimo_barrido_completo"))
        ultima_ok = _parse(self.cache.get("ca_ultima_ejecucion_ok"))
        completo = (
            ultimo_completo is None
            or ultima_ok is None
            or self.ahora - ultimo_completo > timedelta(hours=self.cfg.barrido_completo_horas)
        )
        if completo:
            filtros = {"publicado_desde": _iso(self.ahora - timedelta(days=self.cfg.dias_ventana_compra_agil)).replace("+00:00", "Z")}
        else:
            desde = ultima_ok - timedelta(minutes=30)
            filtros = {
                "cambio_desde": _iso(desde).replace("+00:00", "Z"),
                "cambio_hasta": _iso(self.ahora).replace("+00:00", "Z"),
            }
        log.info("Compra Ágil: barrido %s", "completo" if completo else "incremental")

        encontrados: dict[str, dict] = {}
        for consulta in self.dicc.consultas_compra_agil:
            for bruto in self.ca.buscar(consulta, max_paginas=self.cfg.max_paginas_compra_agil, **filtros):
                if bruto.get("codigo"):
                    encontrados[bruto["codigo"]] = bruto

        detalles = 0
        descartadas = self.cache["ca_descartadas"]
        for codigo, bruto in encontrados.items():
            base = compra_agil_desde_listado(bruto)
            previo = self.items.get(base["id"])
            cambio = base.get("fecha_ultimo_cambio") or ""
            terminos = self.dicc.buscar(base["nombre"])

            if not terminos and not previo and descartadas.get(codigo, {}).get("cambio") == cambio:
                continue  # ya se revisó su detalle y no trata de drones

            cambio_previo = (previo or {}).get("fecha_ultimo_cambio")
            pedir_detalle = previo is None or cambio_previo != cambio or not previo.get("detalle_actualizado")
            if pedir_detalle and detalles < self.cfg.max_detalles_compra_agil and self._con_tiempo():
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

        limite = self.ahora - timedelta(days=self.cfg.dias_ventana_compra_agil + 15)
        self.cache["ca_descartadas"] = {
            c: v for c, v in descartadas.items() if (_parse(v.get("visto")) or self.ahora) > limite
        }
        self.cache["ca_ultima_ejecucion_ok"] = _iso(self.ahora)
        if completo:
            self.cache["ca_ultimo_barrido_completo"] = _iso(self.ahora)
        return {
            "revisadas": len(encontrados),
            "detalles_consultados": detalles,
            "modo": "completo" if completo else "incremental",
        }

    # ------------------------------------------------------------- ejecución

    def _ejecutar_fuente(self, nombre: str, api, funcion) -> dict:
        previo = self.fuentes_previas.get(nombre, {})
        if api is None:
            if self.sin_ticket:
                return {**previo, "ok": False, "mensaje": "Falta el ticket de Mercado Público (MERCADO_PUBLICO_TICKET)."}
            return previo or {"ok": False, "mensaje": "No revisada aún."}
        self.errores_detalle = 0
        try:
            resumen = funcion()
            return {
                "ok": True,
                "mensaje": "OK",
                "ultima_ok": _iso(self.ahora),
                "errores_detalle": self.errores_detalle,
                **resumen,
            }
        except CuotaAgotada as e:
            log.error("%s: %s", nombre, e)
            return {"ok": False, "mensaje": str(e), "ultima_ok": previo.get("ultima_ok")}
        except (ErrorAPI, OSError) as e:
            log.error("%s: %s", nombre, e)
            return {"ok": False, "mensaje": str(e)[:300], "ultima_ok": previo.get("ultima_ok")}

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

    http = ClienteHTTP()
    api_lic = ApiLicitaciones(ticket, http) if ticket and args.solo != "compra_agil" else None
    api_ca = ApiCompraAgil(ticket, ClienteHTTP()) if ticket and args.solo != "licitaciones" else None

    radar = Radar(
        Diccionario.desde_archivo(args.terminos),
        api_lic,
        api_ca,
        _leer_json(args.datos, {}),
        _leer_json(args.cache, {}),
        ahora_utc(),
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
