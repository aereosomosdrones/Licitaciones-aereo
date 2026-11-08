"""Diagnóstico de la API de Compra Ágil: prueba varias formas de consulta y muestra
el código HTTP y el tiempo de respuesta de cada una. No guarda nada.

Uso (requiere el ticket en MERCADO_PUBLICO_TICKET):
    python -m tools.diagnostico_compra_agil
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = "https://api2.mercadopublico.cl/v2/compra-agil"
CODIGO_CONOCIDO = "1388961-170-COT26"


def z(fecha: datetime) -> str:
    return fecha.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def probar(nombre: str, url: str, ticket: str) -> None:
    pedido = urllib.request.Request(url, headers={"ticket": ticket, "Accept": "application/json"})
    inicio = time.monotonic()
    try:
        with urllib.request.urlopen(pedido, timeout=45) as resp:
            cuerpo = resp.read().decode("utf-8", "replace")
            estado = resp.status
    except urllib.error.HTTPError as e:
        cuerpo = e.read().decode("utf-8", "replace")
        estado = e.code
    except Exception as e:  # red, timeout del cliente
        cuerpo = repr(e)
        estado = "sin respuesta"
    segundos = time.monotonic() - inicio
    resumen = cuerpo[:160].replace("\n", " ")
    try:
        datos = json.loads(cuerpo)
        payload = datos.get("payload") or {}
        if isinstance(payload, dict) and "paginacion" in payload:
            resumen = f"total_resultados={payload['paginacion'].get('total_resultados')}"
        elif isinstance(payload, dict) and payload.get("codigo"):
            resumen = f"detalle OK: {payload.get('nombre', '')[:60]}"
    except (ValueError, AttributeError):
        pass
    print(f"{nombre:<45} HTTP {estado!s:<14} {segundos:6.1f} s  {resumen}", flush=True)


def main() -> None:
    ticket = os.environ.get("MERCADO_PUBLICO_TICKET", "").strip()
    if not ticket:
        raise SystemExit("Falta MERCADO_PUBLICO_TICKET")
    ahora = datetime.now(timezone.utc)
    q = lambda **p: f"{BASE}?{urllib.parse.urlencode(p)}"
    pruebas = [
        ("detalle de un código conocido", f"{BASE}/{CODIGO_CONOCIDO}"),
        ("id exacto", q(id=CODIGO_CONOCIDO)),
        ("ttl_cambio_ms 1 h (ejemplo de la guía)", q(ttl_cambio_ms=3600000)),
        ("estado=publicada, 10 por página", q(estado="publicada", tamano_pagina=10)),
        ("region=13, 10 por página", q(region=13, tamano_pagina=10)),
        ("q=dron sin fechas", q(q="dron", tamano_pagina=10)),
        ("q=dron cambios últimas 2 h", q(q="dron", cambio_desde=z(ahora - timedelta(hours=2)), cambio_hasta=z(ahora), tamano_pagina=10)),
        ("q=dron publicados últimos 7 días", q(q="dron", publicado_desde=z(ahora - timedelta(days=7)), publicado_hasta=z(ahora), tamano_pagina=10)),
        ("igual que el radar (+ordenar_por)", q(q="dron", publicado_desde=z(ahora - timedelta(days=7)), publicado_hasta=z(ahora), tamano_pagina=10, numero_pagina=1, ordenar_por="FechaPublicacion")),
        ("q=VANT publicados últimos 7 días", q(q="VANT", publicado_desde=z(ahora - timedelta(days=7)), publicado_hasta=z(ahora), tamano_pagina=10)),
    ]
    print(f"Diagnóstico Compra Ágil · {z(ahora)}")
    for nombre, url in pruebas:
        probar(nombre, url, ticket)
        time.sleep(1)


if __name__ == "__main__":
    main()
