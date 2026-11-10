"""Respaldo manual de Compra Ágil mientras la API no responde.

Dos piezas, ambas sobre GitHub Issues (no requieren credenciales extra: usan el
GITHUB_TOKEN del workflow):

1. Ingreso manual: una persona revisa el buscador público
   (buscador.mercadopublico.cl/compra-agil) y registra cada Compra Ágil con el
   formulario .github/ISSUE_TEMPLATE/compra-agil-manual.yml. El radar lee esos
   issues abiertos y los incorpora al dashboard con origen "manual".

2. Recordatorio: si la API de Compra Ágil está fallando, a las 9, 13 y 17 h de
   Chile se abre un issue asignado al dueño del repositorio (GitHub lo notifica
   por correo) con los pasos de la revisión. Cuando la API vuelve, se cierran.

Uso en el workflow:
    python -m radar.manual recordatorio
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .fuentes import FICHA_COMPRA_AGIL, ZONA_CHILE, a_numero
from .terminos import normalizar

log = logging.getLogger("radar")

PREFIJO_TITULO = "[Compra Ágil manual]"
ETIQUETA_INGRESO = "compra-agil-manual"
ETIQUETA_RECORDATORIO = "recordatorio-compra-agil"
HORAS_RECORDATORIO = (9, 13, 17)  # hora de Chile
BUSCADOR = "https://buscador.mercadopublico.cl/compra-agil"
RAIZ = Path(__file__).resolve().parent.parent
RUTA_DATOS = RAIZ / "docs" / "data" / "licitaciones.json"
RUTA_CACHE = RAIZ / "data" / "cache.json"
RUTA_TERMINOS = RAIZ / "config" / "terminos.json"
PATRON_CODIGO = re.compile(r"\b\d{1,8}-\d{1,6}-COT\d{2}\b", re.IGNORECASE)


# --------------------------------------------------------------------- GitHub


class GitHub:
    def __init__(self, repo: str, token: str, enviar: Callable[..., Any] | None = None):
        self.repo = repo
        self.token = token
        self._enviar = enviar or self._enviar_http

    def _enviar_http(self, metodo: str, ruta: str, datos: dict | None = None) -> Any:
        pedido = urllib.request.Request(
            f"https://api.github.com/repos/{self.repo}{ruta}",
            method=metodo,
            data=json.dumps(datos).encode() if datos is not None else None,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(pedido, timeout=30) as resp:
            cuerpo = resp.read()
            return json.loads(cuerpo) if cuerpo else None

    def issues_abiertos(self) -> list[dict]:
        resultado = []
        for pagina in range(1, 6):
            lote = self._enviar("GET", f"/issues?state=open&per_page=100&page={pagina}") or []
            resultado.extend(i for i in lote if "pull_request" not in i)
            if len(lote) < 100:
                break
        return resultado

    def crear_etiqueta(self, nombre: str, color: str, descripcion: str) -> None:
        try:
            self._enviar("POST", "/labels", {"name": nombre, "color": color, "description": descripcion})
        except urllib.error.HTTPError as e:
            if e.code != 422:  # 422 = ya existe
                raise

    def crear_issue(self, titulo: str, cuerpo: str, etiquetas: list[str], asignados: list[str]) -> dict:
        return self._enviar("POST", "/issues", {"title": titulo, "body": cuerpo, "labels": etiquetas, "assignees": asignados})

    def comentar_y_cerrar(self, numero: int, comentario: str) -> None:
        self._enviar("POST", f"/issues/{numero}/comments", {"body": comentario})
        self._enviar("PATCH", f"/issues/{numero}", {"state": "closed", "state_reason": "completed"})


def github_desde_entorno() -> GitHub | None:
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    return GitHub(repo, token) if repo and token else None


# ------------------------------------------------------- ingreso manual (issues)


def parsear_formulario(cuerpo: str | None) -> dict[str, str]:
    """Cuerpo de un issue creado con formulario: '### Etiqueta' seguido del valor."""
    campos: dict[str, str] = {}
    actual = None
    lineas: list[str] = []
    for linea in (cuerpo or "").splitlines():
        if linea.startswith("### "):
            if actual is not None:
                campos[actual] = "\n".join(lineas).strip()
            actual, lineas = normalizar(linea[4:]), []
        elif actual is not None:
            lineas.append(linea)
    if actual is not None:
        campos[actual] = "\n".join(lineas).strip()
    return {k: ("" if v in ("_No response_", "_Sin respuesta_") else v) for k, v in campos.items()}


def _campo(campos: dict[str, str], *claves: str) -> str:
    for clave in claves:
        for nombre, valor in campos.items():
            if nombre.startswith(clave) and valor:
                return valor
    return ""


def fecha_chile(texto: str) -> str | None:
    """Acepta 'dd-mm-aaaa hh:mm', 'dd/mm/aaaa', 'aaaa-mm-dd hh:mm' (hora de Chile)."""
    texto = (texto or "").strip()
    if not texto:
        return None
    for formato in ("%d-%m-%Y %H:%M", "%d/%m/%Y %H:%M", "%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            fecha = datetime.strptime(texto, formato)
        except ValueError:
            continue
        if "%H" not in formato:
            fecha = fecha.replace(hour=23, minute=59)
        return fecha.replace(tzinfo=ZONA_CHILE).isoformat()
    return None


def item_desde_issue(issue: dict) -> dict | None:
    campos = parsear_formulario(issue.get("body"))
    texto_codigo = _campo(campos, "codigo") or issue.get("title", "")
    encontrado = PATRON_CODIGO.search(texto_codigo)
    if not encontrado:
        log.warning("Issue #%s sin código de Compra Ágil válido; se omite", issue.get("number"))
        return None
    codigo = encontrado.group(0).upper()
    monto = a_numero(re.sub(r"[^\d]", "", _campo(campos, "monto")) or None)
    return {
        "id": f"CA:{codigo}",
        "fuente": "compra_agil",
        "origen": "manual",
        "codigo": codigo,
        "nombre": _campo(campos, "nombre") or issue.get("title", "").replace(PREFIJO_TITULO, "").strip(),
        "organismo": _campo(campos, "organismo"),
        "region": _campo(campos, "region") or None,
        "descripcion": _campo(campos, "descripcion", "notas"),
        "fecha_cierre": fecha_chile(_campo(campos, "fecha de cierre", "cierre")),
        "monto": monto,
        "moneda": "CLP",
        "estado": "abierta",
        "estado_glosa": "Publicada (ingreso manual)",
        "url": FICHA_COMPRA_AGIL.format(codigo=urllib.parse.quote(codigo)),
        "issue_url": issue.get("html_url"),
    }


def es_ingreso_manual(issue: dict) -> bool:
    etiquetas = {e.get("name") for e in issue.get("labels") or []}
    return ETIQUETA_INGRESO in etiquetas or issue.get("title", "").startswith(PREFIJO_TITULO)


def leer_ingresos_manuales(gh: GitHub | None) -> list[dict]:
    if gh is None:
        return []
    try:
        issues = gh.issues_abiertos()
    except (urllib.error.URLError, OSError, ValueError) as e:
        log.warning("No se pudieron leer los ingresos manuales: %s", e)
        return []
    items = [item_desde_issue(i) for i in issues if es_ingreso_manual(i)]
    return [i for i in items if i]


# ------------------------------------------------------------------ recordatorio


def cuerpo_recordatorio(repo: str, terminos: list[str], ultima_ok: str | None, duenio: str) -> str:
    formulario = f"https://github.com/{repo}/issues/new?template=compra-agil-manual.yml"
    lista = "\n".join(f"- [ ] {t}" for t in terminos)
    desde = f" (última revisión automática exitosa: {ultima_ok[:16].replace('T', ' ')} UTC)" if ultima_ok else ""
    return f"""@{duenio} la API de Compra Ágil de ChileCompra no está respondiendo{desde}, así que el radar no puede detectar Compras Ágiles nuevas solo.

**Revisión manual (5 minutos):**

1. Abre el buscador público: {BUSCADOR}
2. Filtra por estado **Publicada** y busca cada término:
{lista}
3. Por cada Compra Ágil de drones que encuentres, regístrala con este formulario (sirve desde el celular): {formulario}
   El radar la agrega al dashboard en su próxima revisión (dentro de la hora), con la etiqueta MANUAL.
4. Cierra este aviso cuando termines.

Este recordatorio se repite a las 9:00, 13:00 y 17:00 (hora de Chile) mientras la API siga caída, y se cierra solo cuando vuelva.
"""


def gestionar_recordatorio(gh: GitHub, datos: dict, cache: dict, terminos: list[str], ahora: datetime, duenio: str) -> dict:
    """Abre el recordatorio del bloque horario si corresponde; cierra los abiertos si la API volvió."""
    ca = (datos.get("fuentes") or {}).get("compra_agil") or {}
    abiertos = [n for n in cache.get("recordatorios_abiertos", [])]

    if ca.get("ok"):
        for numero in abiertos:
            try:
                gh.comentar_y_cerrar(numero, "La API de Compra Ágil volvió a responder: el radar vuelve a detectarlas solo. ✅")
            except (urllib.error.URLError, OSError) as e:
                log.warning("No se pudo cerrar el recordatorio #%s: %s", numero, e)
        return {**cache, "recordatorios_abiertos": []}

    local = ahora.astimezone(ZONA_CHILE)
    bloque = max((h for h in HORAS_RECORDATORIO if h <= local.hour), default=None)
    if bloque is None:
        return cache
    clave = f"{local:%Y-%m-%d} {bloque:02d}"
    if cache.get("ultimo_recordatorio") == clave:
        return cache

    gh.crear_etiqueta(ETIQUETA_RECORDATORIO, "ECE31D", "Recordatorio de revisión manual de Compra Ágil")
    gh.crear_etiqueta(ETIQUETA_INGRESO, "104A7F", "Compra Ágil ingresada a mano desde el buscador público")
    # Un solo recordatorio abierto a la vez: el anterior se cierra.
    for numero in abiertos:
        try:
            gh.comentar_y_cerrar(numero, "Reemplazado por el recordatorio siguiente.")
        except (urllib.error.URLError, OSError) as e:
            log.warning("No se pudo cerrar el recordatorio #%s: %s", numero, e)
    titulo = f"Revisión manual Compra Ágil · {local:%d-%m-%Y} {bloque:02d}:00"
    issue = gh.crear_issue(titulo, cuerpo_recordatorio(gh.repo, terminos, ca.get("ultima_ok"), duenio), [ETIQUETA_RECORDATORIO], [duenio])
    log.info("Recordatorio de revisión manual creado: #%s", issue.get("number"))
    return {**cache, "ultimo_recordatorio": clave, "recordatorios_abiertos": [issue["number"]]}


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = argv if argv is not None else sys.argv[1:]
    if args[:1] != ["recordatorio"]:
        print(__doc__)
        return 2
    gh = github_desde_entorno()
    if gh is None:
        log.error("Faltan GITHUB_TOKEN o GITHUB_REPOSITORY")
        return 1
    datos = json.loads(RUTA_DATOS.read_text(encoding="utf-8"))
    cache = json.loads(RUTA_CACHE.read_text(encoding="utf-8"))
    terminos = json.loads(RUTA_TERMINOS.read_text(encoding="utf-8")).get("consultas_compra_agil", [])
    duenio = gh.repo.split("/")[0]
    nuevo = gestionar_recordatorio(gh, datos, cache, terminos, datetime.now(timezone.utc), duenio)
    if nuevo is not cache:
        RUTA_CACHE.write_text(json.dumps(nuevo, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
