"""Relaciona cada proceso con el área del ecosistema AÉREO que podría atenderlo.

Las áreas y su numeración son las de la identidad de AÉREO (sellos de aereo.cl):

    01 Capacitación  · AÉREO Empresas   cursos, credencial DGAC, vuelo nocturno
    02 Servicios     · AÉREO Servicios  el vuelo hecho: fotogrametría, inspección, LiDAR…
    03 Asesorías     · AÉREO Certifica  asesoría AOC / CEO ante la DGAC
    03 Asesorías     · AÉREO Gestión    operación delegada: flota, mantención, reparación
    04 Venta         · AÉREO Tienda     drones, baterías, accesorios (próximamente)
    —  Otros                            seguros y lo que queda fuera del ecosistema

Se decide por lo PRIMERO que pide el título: "Curso de inspección con drones" es
capacitación; "Servicio de inspección con drones y capacitación" es un servicio;
"Adquisición de dron con curso incluido" es una venta de equipo.
"""

from __future__ import annotations

import re

from .terminos import normalizar

AREAS = {
    "capacitacion": {"numero": "01", "base": "Capacitación", "sello": "Empresas", "icono": "empresas", "orden": 1},
    "servicios": {"numero": "02", "base": "Servicios", "sello": "Servicios", "icono": "servicios", "orden": 2},
    "certifica": {"numero": "03", "base": "Asesorías", "sello": "Certifica", "icono": "certifica", "orden": 3},
    "gestion": {"numero": "03", "base": "Asesorías", "sello": "Gestión", "icono": "gestion", "orden": 4},
    "tienda": {"numero": "04", "base": "Venta de drones", "sello": "Tienda", "icono": "tienda", "orden": 5},
    "otros": {"numero": "—", "base": "Otros", "sello": None, "icono": None, "orden": 6},
}

_PALABRAS = {
    "certifica": r"asesoria.{0,80}(\baoc\b|\bceo\b|dgac|certificacion|acreditacion)|\baoc\b|certificado de operador|manual(es)? de operaciones",
    "capacitacion": (
        r"\bcursos?\b|capacitaci|capacitar|capaxitacion|\btaller(es)?\b|diplomado|formacion|entrenamiento|adiestramiento"
        r"|credencial|licencia|habilitacion de vuelo|vuelo nocturno|certificacion|acreditacion|cuotas? de inscripcion"
    ),
    "otros": r"\bseguros?\b|\bseguiro\b|poliza|responsabilidad civil",
    "gestion": r"mantencion|mantenimiento|reparacion|\breparar\b|diagnostico|calibracion|servicio tecnico|revision tecnica",
    "servicios": (
        r"levantamiento|fotogrametri|ortomosaico|ortofoto|topografi|inspeccion|termografi|filmacion|grabacion"
        r"|fotografia|registro (aereo|audiovisual|con dron)|produccion audiovisual|fumigacion|pulverizacion"
        r"|aplicacion (aerea|agricola)|monitoreo|vigilancia|patrullaje|sobrevuelo|\bshow\b|espectaculo|arriendo"
        r"|horas de vuelo|con piloto|servicios? de (dron|drone|drones|rpas?|vuelo)|\bestudio\b"
    ),
}
_PATRONES = {area: re.compile(patron) for area, patron in _PALABRAS.items()}
_COMPRA = re.compile(r"^(?:\S+ ){0,3}?(adquisicion|adquirir|adq|compra|comprar|suministro|provision)\b")
_EQUIPO = re.compile(
    r"\b(dron|drone|drones|rpas?|uav|aeronaves?|kit|equipos?|equipamiento|baterias?|accesorios?|repuestos?|helices?"
    r"|paracaidas?|control remoto|camaras?|escaner|estacion|receptor|tarjetas?|landing|tablet|software|materiales|elementos)\b"
)
_SERVICIO = re.compile(r"\bservicios?\b|\bsvc\b|\bserv\b|contratacion|\bcontratar\b")


def _primera(area: str, texto: str) -> int | None:
    m = _PATRONES[area].search(texto)
    return m.start() if m else None


def _clasificar_texto(texto: str) -> str | None:
    if not texto:
        return None
    if _primera("certifica", texto) is not None:
        return "certifica"
    pos = {a: _primera(a, texto) for a in ("capacitacion", "otros", "gestion", "servicios")}
    pos = {a: p for a, p in pos.items() if p is not None}
    equipo = _EQUIPO.search(texto)
    servicio = _SERVICIO.search(texto)
    if _COMPRA.search(texto):
        # Se compra lo primero que se nombra: un curso, un seguro, un servicio… o un equipo.
        antes_del_equipo = {a: p for a, p in pos.items() if equipo is None or p < equipo.start()}
        if antes_del_equipo:
            return min(antes_del_equipo, key=antes_del_equipo.get)
        if servicio and (equipo is None or servicio.start() < equipo.start()):
            return min(pos, key=pos.get) if pos else "servicios"
        return "tienda"
    if equipo and len(texto[: equipo.start()].split()) <= 3:
        # El título parte nombrando el equipo ("Dron para…", "N° 8372 Dron…"): es una venta,
        # salvo que antes pida otra cosa ("Seguro para dron", "Curso de dron").
        if not any(p < equipo.start() for p in pos.values()) and not (servicio and servicio.start() < equipo.start()):
            return "tienda"
    if pos:
        return min(pos, key=pos.get)
    if servicio:
        return "servicios"
    if equipo:
        return "tienda"
    return None


def clasificar(item: dict) -> str:
    """Área del ecosistema AÉREO para un proceso (clave de AREAS)."""
    area = _clasificar_texto(normalizar(item.get("nombre")))
    if area:
        return area
    # El título no dice qué se pide: se mira lo que sí habla de drones en el detalle.
    lineas = [normalizar(x) for x in [*(item.get("productos") or []), *(item.get("adjuntos") or []), item.get("descripcion") or ""]]
    for linea in lineas:
        area = _clasificar_texto(linea)
        if area:
            return area
    return "tienda"
