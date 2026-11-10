"""Explora el buscador público de Compra Ágil (buscador.mercadopublico.cl) con un
navegador real y muestra qué peticiones de datos hace la página y qué responden.

Sirve para diseñar el respaldo del radar cuando la API de Compra Ágil no responde.
No guarda nada en el repositorio; deja un resumen en el log y archivos de muestra
en la carpeta indicada (para subirlos como artefacto del workflow).

Uso:
    python -m tools.explorar_buscador [carpeta_salida]
"""

import asyncio
import json
import sys
from pathlib import Path

from playwright.async_api import async_playwright

PAGINAS = [
    "https://buscador.mercadopublico.cl/compra-agil",
    "https://buscador.mercadopublico.cl/compra-agil?keywords=dron",
]


async def explorar(salida: Path) -> None:
    salida.mkdir(parents=True, exist_ok=True)
    capturas = []

    async with async_playwright() as p:
        navegador = await p.chromium.launch()
        pagina = await navegador.new_page(locale="es-CL")

        async def al_responder(resp):
            req = resp.request
            if req.resource_type not in ("xhr", "fetch"):
                return
            item = {
                "url": resp.url,
                "metodo": req.method,
                "estado": resp.status,
                "cabeceras_peticion": {
                    k: ("<omitida>" if k.lower() in ("cookie", "authorization") else v)
                    for k, v in req.headers.items()
                },
                "cuerpo_peticion": req.post_data,
            }
            try:
                cuerpo = await resp.text()
                item["largo"] = len(cuerpo)
                item["muestra"] = cuerpo[:1500]
            except Exception as e:  # respuestas sin cuerpo
                item["muestra"] = f"<sin cuerpo: {e}>"
            capturas.append(item)

        pagina.on("response", lambda r: asyncio.ensure_future(al_responder(r)))

        for n, url in enumerate(PAGINAS):
            print(f"\n=== Abriendo {url}", flush=True)
            try:
                await pagina.goto(url, wait_until="networkidle", timeout=90_000)
            except Exception as e:
                print(f"  (aviso al cargar: {e})")
            await pagina.wait_for_timeout(8000)
            await pagina.screenshot(path=str(salida / f"pagina-{n}.png"), full_page=True)
            texto = await pagina.inner_text("body")
            (salida / f"texto-{n}.txt").write_text(texto, encoding="utf-8")
            print(f"  Texto visible (primeros 1200 caracteres):\n{texto[:1200]}")

        # Probar el cuadro de búsqueda, si existe.
        for selector in ['input[type="search"]', 'input[placeholder*="usca"]', "input[type=text]"]:
            campo = await pagina.query_selector(selector)
            if campo:
                print(f"\n=== Buscando 'dron' en {selector}", flush=True)
                await campo.fill("dron")
                await campo.press("Enter")
                await pagina.wait_for_timeout(12000)
                await pagina.screenshot(path=str(salida / "busqueda-dron.png"), full_page=True)
                texto = await pagina.inner_text("body")
                (salida / "texto-busqueda.txt").write_text(texto, encoding="utf-8")
                print(f"  URL tras buscar: {pagina.url}")
                print(f"  Texto visible:\n{texto[:1500]}")
                break

        await navegador.close()

    (salida / "peticiones.json").write_text(json.dumps(capturas, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n=== {len(capturas)} peticiones de datos (xhr/fetch):")
    for c in capturas:
        print(f"\n[{c['estado']}] {c['metodo']} {c['url']}")
        cab = {k: v for k, v in c["cabeceras_peticion"].items() if k.lower() not in ("user-agent", "accept-language", "referer", "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform")}
        print(f"  cabeceras: {json.dumps(cab, ensure_ascii=False)[:600]}")
        if c.get("cuerpo_peticion"):
            print(f"  cuerpo: {c['cuerpo_peticion'][:400]}")
        print(f"  respuesta ({c.get('largo', '?')} bytes): {c['muestra'][:700]}")


if __name__ == "__main__":
    asyncio.run(explorar(Path(sys.argv[1] if len(sys.argv) > 1 else "exploracion")))
