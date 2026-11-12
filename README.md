# Radar de Licitaciones · Aéreo

Monitor automático de **Mercado Público** (Licitaciones) y **Compra Ágil** que detecta
procesos de compra relacionados con drones y sus servicios derivados, y los
muestra en un dashboard para [aereo.cl](https://www.aereo.cl) que **se actualiza solo cada hora**.

## Qué busca

Términos configurados en [`config/terminos.json`](config/terminos.json):

| Núcleo | Derivados |
|---|---|
| dron / drones, RPA / RPAS, UAV / UAS, VANT, aeronave no tripulada / pilotada a distancia, multirrotor / cuadricóptero / VTOL, marcas (DJI, Mavic, Matrice, Autel EVO…), credencial / piloto de drones, DAN 151, antidrones | aerofotogrametría, ortomosaico / ortofoto, LiDAR, servicios aéreos (filmación, fotografía, inspección, termografía, levantamiento, fumigación… aérea o con dron), multiespectral |

Se buscan sin importar tildes ni mayúsculas, y tienen **exclusiones** para evitar
falsos positivos (p. ej. "RPA" de *automatización robótica de procesos* / UiPath,
"plan piloto", escáneres automotrices Autel, cámaras DJI Osmo).

## Cómo funciona

Cada hora, [`.github/workflows/radar.yml`](.github/workflows/radar.yml) ejecuta `python -m radar.monitor`:

1. **Licitaciones** (API v1): descarga todas las licitaciones activas y revisa el nombre de
   todas. Además revisa la **descripción y los ítems** de cada licitación nueva (hasta 250
   por hora; la primera vez tarda algunas horas en cubrir todas las activas). Las que ya
   sigue se refrescan cada 6 h hasta que se adjudican, quedan desiertas o se revocan.
2. **Compra Ágil** (API v2), en tres pasos:
   - **Consultas precisas** (dron, RPAS, RPA, DJI…) sobre lo publicado en las últimas 72 h, cada
     hora; una vez al día, sobre los 45 días completos. Las fechas de los filtros van en **hora
     de Chile**: la API ignora la zona, y una ventana en UTC queda 3 h adelantada y no devuelve
     lo recién publicado (ese defecto atrasó las detecciones hasta 24 h entre oct. y nov. de 2026).
   - **Segunda pasada**: consultas amplias de capacitación (`consultas_amplias`: curso,
     capacitación, DGAC, credencial…). Se abre el detalle de cada resultado y solo se guarda si
     los productos o los nombres de los adjuntos hablan de drones. Así aparecen las compras
     cuyo título no lo dice («6 cuotas de inscripción de curso», «Departamento de geología»).
     Las que solo dicen «capacitación según archivo adjunto» quedan en la lista **por revisar**
     del dashboard: la API no permite leer los adjuntos.
   - **Resultados**: a las compras cerradas se les consulta el detalle hasta que Mercado Público
     publica las ofertas; se guarda cuántas hubo, quién fue seleccionado y si AÉREO cotizó.
3. **Relaciona cada proceso con el ecosistema AÉREO** ([`radar/areas.py`](radar/areas.py)):
   01 Capacitación (AÉREO Empresas), 02 Servicios, 03 Asesorías (Certifica, Gestión),
   04 Venta de drones (Tienda) u «otros» (seguros). Y le pone relevancia: **alta** si el título
   o lo que se pide habla de drones, **baja** si es solo una mención al pasar.
4. Guarda el resultado en [`docs/data/licitaciones.json`](docs/data/licitaciones.json)
   (hace commit en el repo, así queda historial). El dashboard publicado en Netlify
   (licitaciones.aereo.cl) lee ese archivo directamente desde GitHub.
5. **Avisa por correo**: cada oportunidad nueva de las áreas de `avisar_areas`
   (`config/terminos.json`; por defecto capacitación, asesorías y servicios) abre un issue
   asignado al dueño del repo, que GitHub envía por correo, y lo cierra cuando la compra cierra.
   Con `"avisar_areas": []` no se envían avisos.

El dashboard ([`docs/index.html`](docs/index.html), HTML estático sin dependencias) usa el
sistema gráfico de AÉREO y muestra:

- Qué hay para cotizar: abiertas, las que cierran en 24 h, nuevas y presupuesto abierto.
- Las áreas del ecosistema con su sello: un clic filtra todo el tablero por área.
- La lista agrupada por urgencia (cierran hoy, mañana, esta semana), con búsqueda, filtros y CSV.
  Las menciones indirectas quedan ocultas salvo que se marque «Incluir menciones indirectas».
- **Cómo nos fue**: adjudicadas, cobertura (cuántas cotizó AÉREO), efectividad, quién se las
  adjudicó y las últimas adjudicadas con la oferta de AÉREO y su lugar en precio.
- Se refresca sola: consulta datos nuevos cada 10 minutos.

## Puesta en marcha (una sola vez)

1. **Pedir el ticket de la API** en <https://www.chilecompra.cl/api/> ("Pide tu ticket",
   con Clave Única). Llega por correo. El mismo ticket sirve para Licitaciones y Compra Ágil.
2. **Guardarlo como secreto** del repositorio: *Settings → Secrets and variables → Actions →
   New repository secret*, nombre `MERCADO_PUBLICO_TICKET`.
   No lo pegues en el código ni en issues.
3. **Mergear a `main`** y, en *Actions → Radar de licitaciones → Run workflow*, lanzarlo una
   vez a mano.

> **Disparo horario.** GitHub no garantiza las ejecuciones programadas por `cron` (en este repo
> no llegaron a dispararse). Por eso cada ejecución termina con el job `relanzar`, que espera
> hasta el minuto 5 de la hora siguiente y lanza la próxima revisión, salvo que ya exista otra
> ejecución más reciente (la del cron o la de un push). Si la cadena se corta (p. ej. GitHub
> cancela una ejecución), basta con un *Run workflow* manual para retomarla.

## Respaldo manual de Compra Ágil

La API de Compra Ágil de ChileCompra es beta y puede caerse (desde el 6/7-nov-2026 responde
504 para todos). El buscador público bloquea consultas automáticas desde servidores, así que el
respaldo es una revisión manual guiada:

- **Recordatorio:** mientras la API falle, a las 9:00, 13:00 y 17:00 (hora de Chile) el radar abre
  un issue asignado al dueño del repo (llega por correo) con el enlace al buscador público y los
  términos a buscar. Al abrirse uno nuevo se cierra el anterior; cuando la API vuelve, se cierran todos.
- **Ingreso:** cada Compra Ágil encontrada se registra con el formulario
  *Issues → New issue → Agregar Compra Ágil (revisión manual)*. En la siguiente revisión horaria
  aparece en el dashboard con la etiqueta **MANUAL**. Si después la API la trae, mandan los datos
  oficiales.
- **Diagnóstico:** *Actions → Diagnóstico API Compra Ágil → Run workflow* muestra si la API ya responde.

## Diseño y acceso

- El dashboard usa la identidad de AÉREO vigente (repo `aereo-web`, `partials/chrome.css`,
  26-10-2026): amarillo `#ECE31D`, negro, petróleo `#425D76` y blanco, sin rojo ni verde (los
  estados se dicen con palabras); Big Shoulders Display, Roboto y Roboto Mono; logo horizontal
  y sellos de área copiados en `docs/assets/`.
- En Netlify (proyecto `aereo-licitaciones`, licitaciones.aereo.cl) el sitio está protegido con
  la contraseña de visitantes de Netlify (*Project configuration → Access & security → Visitor
  access*). Los datos de `docs/data/licitaciones.json` son información pública de Mercado Público.

## Publicar en Netlify (en tu web)

El repo incluye [`netlify.toml`](netlify.toml): publica la carpeta `docs/` sin compilar nada.

1. En <https://app.netlify.com> → **Add new project → Import an existing project → GitHub**
   y elige `Licitaciones-aereo`. Deja los valores que propone (publish directory `docs`,
   sin build command) y pulsa **Deploy**.
2. **Domain management → Add a domain**: por ejemplo `licitaciones.aereo.cl`, y crea en el
   DNS de aereo.cl el registro CNAME que Netlify indique. También se puede incrustar en una
   página de aereo.cl con `<iframe src="https://licitaciones.aereo.cl" style="width:100%;height:100vh;border:0"></iframe>`.

Las publicaciones de Netlify consumen créditos (15 por publicación en el plan gratis), así que
**los commits horarios de datos no republican el sitio**: la página lee los datos frescos
directamente desde GitHub (`<meta name="radar-datos">` en `docs/index.html`). Eso requiere que
el repositorio sea **público** (el ticket sigue protegido como secreto). Con el repo privado,
define en Netlify la variable `RADAR_PUBLICAR_DATOS=si` para republicar con cada actualización
(solo conviene en cuentas antiguas de Netlify con minutos de build).

## Ejecutar localmente

```bash
export MERCADO_PUBLICO_TICKET=tu-ticket
python3 -m radar.monitor            # o --solo licitaciones / --solo compra_agil
python3 -m http.server -d docs 8000 # abrir http://localhost:8000
```

Requiere Python 3.10+ y no tiene dependencias externas. Para correrlo cada hora en un
servidor propio en vez de GitHub Actions: `7 * * * * cd /ruta/al/repo && python3 -m radar.monitor`
y sirve la carpeta `docs/` con cualquier servidor web.

Tests: `python3 -m unittest discover -s tests -t .`

## Ajustes

- **Términos**: edita `config/terminos.json`. `patron` es una expresión regular sobre el texto
  en minúsculas y sin tildes; `excluir_si` descarta la coincidencia de ese término cuando
  aparece alguna de esas frases; `consultas_compra_agil` son las palabras que se envían al
  buscador de Compra Ágil.
- **Ritmo y límites** (detalles por hora, ventana de días, retención): clase `Config` en
  [`radar/monitor.py`](radar/monitor.py).

## Estructura

```
config/terminos.json        términos, patrones y exclusiones
radar/terminos.py           normalización y detección de términos
radar/areas.py              relación de cada proceso con las áreas del ecosistema AÉREO
radar/manual.py             avisos por correo, recordatorios e ingreso manual (GitHub Issues)
radar/fuentes.py            clientes de las APIs (Licitaciones v1, Compra Ágil v2)
radar/monitor.py            orquestación, fusión con datos previos, JSON de salida
docs/index.html             dashboard
docs/data/licitaciones.json datos publicados (los escribe el radar)
data/cache.json             estado interno (qué ya se revisó)
tests/                      tests con respuestas simuladas de las APIs
```
