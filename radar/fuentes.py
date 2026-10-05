"""Clientes de las APIs de Mercado Público (Licitaciones v1 y Compra Ágil v2).

Documentación oficial: https://www.chilecompra.cl/api/
- Licitaciones: https://api.mercadopublico.cl/servicios/v1/publico/licitaciones.json
  (ticket en la query string).
- Compra Ágil:  https://api2.mercadopublico.cl/v2/compra-agil
  (ticket en el header HTTP "ticket"; respuesta envuelta en {"success", "payload"}).
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable

try:
    from zoneinfo import ZoneInfo

    ZONA_CHILE = ZoneInfo("America/Santiago")
except Exception:  # pragma: no cover - sin tzdata se usa UTC-3 fijo
    from datetime import timedelta

    ZONA_CHILE = timezone(timedelta(hours=-3))

log = logging.getLogger("radar")

URL_LICITACIONES = "https://api.mercadopublico.cl/servicios/v1/publico/licitaciones.json"
URL_COMPRA_AGIL = "https://api2.mercadopublico.cl/v2/compra-agil"
FICHA_LICITACION = "https://www.mercadopublico.cl/Procurement/Modules/RFB/DetailsAcquisition.aspx?idlicitacion={codigo}"
FICHA_COMPRA_AGIL = "https://buscador.mercadopublico.cl/ficha?code={codigo}"

ESTADOS_LICITACION = {
    5: ("abierta", "Publicada"),
    6: ("cerrada", "Cerrada"),
    7: ("desierta", "Desierta"),
    8: ("adjudicada", "Adjudicada"),
    18: ("revocada", "Revocada"),
    19: ("suspendida", "Suspendida"),
}
ESTADOS_COMPRA_AGIL = {
    "publicada": "abierta",
    "cerrada": "cerrada",
    "desierta": "desierta",
    "cancelada": "cancelada",
    "proveedor_seleccionado": "adjudicada",
    "oc_emitida": "adjudicada",
}
ESTADOS_FINALES = {"adjudicada", "desierta", "cancelada", "revocada"}


class ErrorAPI(Exception):
    pass


class CuotaAgotada(ErrorAPI):
    pass


# --------------------------------------------------------------------------- HTTP


def http_get_json(url: str, headers: dict[str, str] | None = None, timeout: int = 90) -> Any:
    """GET que devuelve JSON. Lanza urllib.error.HTTPError con el cuerpo leído en .cuerpo."""
    pedido = urllib.request.Request(url, headers={"Accept": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(pedido, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8-sig"))
    except urllib.error.HTTPError as e:
        try:
            e.cuerpo = e.read().decode("utf-8", "replace")
        except Exception:
            e.cuerpo = ""
        raise


def _sin_ticket(texto: str) -> str:
    """Nunca dejar el ticket en logs ni en el JSON publicado."""
    return re.sub(r"ticket=[^&\s]+", "ticket=***", texto) if texto else texto


class ClienteHTTP:
    """GET con reintentos, pausa entre llamadas y manejo de cuota."""

    def __init__(
        self,
        obtener: Callable[..., Any] = http_get_json,
        pausa: float = 0.5,
        reintentos: int = 3,
        dormir: Callable[[float], None] = time.sleep,
    ):
        self._obtener = obtener
        self.pausa = pausa
        self.reintentos = reintentos
        self._dormir = dormir
        self.llamadas = 0

    def get(self, url: str, headers: dict[str, str] | None = None) -> Any:
        ultimo_error: Exception | None = None
        for intento in range(1, self.reintentos + 1):
            if self.llamadas:
                self._dormir(self.pausa)
            self.llamadas += 1
            try:
                return self._obtener(url, headers=headers)
            except urllib.error.HTTPError as e:
                cuerpo = getattr(e, "cuerpo", "")
                if e.code == 429:
                    raise CuotaAgotada("Cuota de la API agotada (HTTP 429). Se reintentará en la próxima ejecución.")
                if e.code in (401, 403):
                    raise ErrorAPI(f"Ticket rechazado (HTTP {e.code}). Revisa el secreto MERCADO_PUBLICO_TICKET.")
                if e.code == 404:
                    return None
                ultimo_error = ErrorAPI(f"HTTP {e.code}: {_sin_ticket(cuerpo)[:200]}")
                if e.code < 500:
                    raise ultimo_error
            except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as e:
                ultimo_error = ErrorAPI(f"Error de red: {_sin_ticket(str(e))[:200]}")
            log.warning("Intento %s/%s fallido: %s", intento, self.reintentos, ultimo_error)
            self._dormir(min(30, 2**intento))
        raise ultimo_error or ErrorAPI("Error desconocido")


# ------------------------------------------------------------------ fechas/montos


def fecha_iso(valor: str | None) -> str | None:
    """Normaliza fechas de ambas APIs a ISO-8601 con zona horaria.

    La API de Licitaciones entrega hora local de Chile sin zona
    ("2026-10-10T15:00:00" o con fracciones); se le asigna America/Santiago.
    """
    if not valor or not isinstance(valor, str):
        return None
    texto = valor.strip().replace("Z", "+00:00")
    if "." in texto:
        base, _, resto = texto.partition(".")
        zona = ""
        for signo in ("+", "-"):
            if signo in resto:
                zona = signo + resto.split(signo, 1)[1]
        texto = base + zona
    try:
        fecha = datetime.fromisoformat(texto)
    except ValueError:
        return None
    if fecha.tzinfo is None:
        fecha = fecha.replace(tzinfo=ZONA_CHILE)
    return fecha.isoformat()


def a_numero(valor: Any) -> float | None:
    if valor in (None, "", 0, "0"):
        return None
    try:
        return float(valor)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ Licitaciones


class ApiLicitaciones:
    def __init__(self, ticket: str, http: ClienteHTTP):
        self.ticket = ticket
        self.http = http

    def _get(self, **params: str) -> dict:
        url = f"{URL_LICITACIONES}?{urllib.parse.urlencode({**params, 'ticket': self.ticket})}"
        for intento in range(3):
            datos = self.http.get(url)
            if datos is None:
                return {"Listado": []}
            if isinstance(datos, dict) and "Listado" not in datos and "Codigo" in datos:
                # Error de negocio: {"Codigo": 10500, "Mensaje": "...peticiones simultáneas..."}
                if datos.get("Codigo") == 10500 and intento < 2:
                    self.http._dormir(5)
                    continue
                if datos.get("Codigo") in (203, 10200):
                    raise ErrorAPI(f"Ticket rechazado por la API de Licitaciones: {datos.get('Mensaje')}")
                raise ErrorAPI(f"API Licitaciones {datos.get('Codigo')}: {datos.get('Mensaje')}")
            return datos
        raise ErrorAPI("API Licitaciones ocupada (peticiones simultáneas)")

    def activas(self) -> list[dict]:
        return self._get(estado="activas").get("Listado") or []

    def detalle(self, codigo: str) -> dict | None:
        listado = self._get(codigo=codigo).get("Listado") or []
        return listado[0] if listado else None


def licitacion_desde_resumen(r: dict) -> dict:
    estado, glosa = ESTADOS_LICITACION.get(r.get("CodigoEstado"), ("otro", "Desconocido"))
    codigo = r["CodigoExterno"]
    return {
        "id": f"LIC:{codigo}",
        "fuente": "licitacion",
        "codigo": codigo,
        "nombre": (r.get("Nombre") or "").strip(),
        "estado": estado,
        "estado_glosa": glosa,
        "fecha_cierre": fecha_iso(r.get("FechaCierre")),
        "url": FICHA_LICITACION.format(codigo=urllib.parse.quote(codigo)),
    }


def licitacion_desde_detalle(d: dict) -> dict:
    item = licitacion_desde_resumen(d)
    comprador = d.get("Comprador") or {}
    fechas = d.get("Fechas") or {}
    productos = ((d.get("Items") or {}).get("Listado")) or []
    item.update(
        {
            "descripcion": (d.get("Descripcion") or "").strip(),
            "organismo": (comprador.get("NombreOrganismo") or "").strip(),
            "unidad": (comprador.get("NombreUnidad") or "").strip(),
            "region": (comprador.get("RegionUnidad") or "").strip() or None,
            "comuna": (comprador.get("ComunaUnidad") or "").strip() or None,
            "fecha_publicacion": fecha_iso(fechas.get("FechaPublicacion") or fechas.get("FechaCreacion")),
            "fecha_cierre": fecha_iso(fechas.get("FechaCierre")) or item["fecha_cierre"],
            "fecha_adjudicacion": fecha_iso(fechas.get("FechaAdjudicacion")),
            "monto": a_numero(d.get("MontoEstimado")),
            "moneda": d.get("Moneda") or "CLP",
            "tipo": d.get("Tipo"),
            "productos": [
                " — ".join(x for x in (p.get("NombreProducto"), p.get("Descripcion")) if x)
                for p in productos
            ][:15],
        }
    )
    return item


# ------------------------------------------------------------------ Compra Ágil


class ApiCompraAgil:
    def __init__(self, ticket: str, http: ClienteHTTP, tamano_pagina: int = 50):
        self.ticket = ticket
        self.http = http
        self.tamano_pagina = tamano_pagina

    def _get(self, url: str) -> Any:
        datos = self.http.get(url, headers={"ticket": self.ticket})
        if datos is None:
            return None
        if isinstance(datos, dict) and datos.get("success") == "NOK":
            errores = datos.get("errors") or [{}]
            raise ErrorAPI(f"API Compra Ágil: {errores[0].get('mensaje', 'error')}")
        return datos.get("payload") if isinstance(datos, dict) and "payload" in datos else datos

    def buscar(self, q: str, max_paginas: int = 5, **filtros: str) -> list[dict]:
        items: list[dict] = []
        for pagina in range(1, max_paginas + 1):
            params = {
                "q": q,
                **{k: v for k, v in filtros.items() if v},
                "tamano_pagina": self.tamano_pagina,
                "numero_pagina": pagina,
                "ordenar_por": "FechaPublicacion",
            }
            payload = self._get(f"{URL_COMPRA_AGIL}?{urllib.parse.urlencode(params)}") or {}
            items.extend(payload.get("items") or [])
            total_paginas = (payload.get("paginacion") or {}).get("total_paginas") or 0
            if pagina >= total_paginas:
                break
        return items

    def detalle(self, codigo: str) -> dict | None:
        return self._get(f"{URL_COMPRA_AGIL}/{urllib.parse.quote(codigo)}")


def compra_agil_desde_listado(i: dict) -> dict:
    estado_api = (i.get("estado") or {}).get("codigo") or ""
    fechas = i.get("fechas") or {}
    montos = i.get("montos") or i.get("presupuesto") or {}
    inst = i.get("institucion") or {}
    codigo = i["codigo"]
    monto = a_numero(montos.get("monto_disponible_clp")) or a_numero(montos.get("monto_disponible"))
    return {
        "id": f"CA:{codigo}",
        "fuente": "compra_agil",
        "codigo": codigo,
        "nombre": (i.get("nombre") or "").strip(),
        "estado": ESTADOS_COMPRA_AGIL.get(estado_api, "otro"),
        "estado_glosa": (i.get("estado") or {}).get("glosa") or estado_api,
        "fecha_publicacion": fecha_iso(fechas.get("fecha_publicacion")),
        "fecha_cierre": fecha_iso(fechas.get("fecha_cierre")),
        "fecha_ultimo_cambio": fechas.get("fecha_ultimo_cambio"),
        "organismo": (inst.get("organismo_comprador") or "").strip(),
        "unidad": (inst.get("unidad_compra") or "").strip(),
        "region": inst.get("nombre_region"),
        "monto": monto,
        "moneda": "CLP" if a_numero(montos.get("monto_disponible_clp")) else (montos.get("moneda") or "CLP"),
        "ofertas": (i.get("resumen") or {}).get("total_ofertas_recibidas"),
        "url": FICHA_COMPRA_AGIL.format(codigo=urllib.parse.quote(codigo)),
    }


def compra_agil_desde_detalle(d: dict) -> dict:
    item = compra_agil_desde_listado(d)
    entrega = d.get("entrega") or {}
    item.update(
        {
            "descripcion": (d.get("descripcion") or "").strip(),
            "direccion_entrega": entrega.get("direccion_entrega"),
            "plazo_entrega_dias": entrega.get("plazo_entrega_dias"),
            "productos": [
                " — ".join(x for x in (p.get("nombre"), p.get("descripcion")) if x)
                for p in (d.get("productos_solicitados") or [])
            ][:15],
        }
    )
    return item


def ahora_utc() -> datetime:
    return datetime.now(timezone.utc)
