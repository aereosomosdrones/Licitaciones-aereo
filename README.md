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
2. **Compra Ágil** (API v2): consulta el buscador oficial con cada término y verifica cada
   resultado contra los patrones (si no aparece en el nombre, revisa la descripción y los
   productos solicitados). Barrido completo de los últimos 45 días cada 12 h y, entre medio,
   solo los cambios recientes, para cuidar la cuota del ticket.
3. Guarda el resultado en [`docs/data/licitaciones.json`](docs/data/licitaciones.json)
   (hace commit en el repo, así queda historial) y publica el dashboard en GitHub Pages.

El dashboard ([`docs/index.html`](docs/index.html), HTML estático sin dependencias) muestra:

- Abiertas ahora, nuevas en las últimas 24 h, las que cierran en ≤ 7 días y el monto abierto.
- Procesos abiertos por término (clic para filtrar) y publicados por semana.
- Lista filtrable por texto, fuente, estado, región y término, con cuenta regresiva al cierre
  (rojo < 48 h, amarillo < 7 días), enlace directo a la ficha oficial y descripción.
- Exportación a CSV (abre directo en Excel).
- Se refresca sola: consulta datos nuevos cada 10 minutos, avisa con un mensaje cuando hay
  oportunidades nuevas y, si se activa "🔔 Avisarme", con una notificación del navegador.

## Puesta en marcha (una sola vez)

1. **Pedir el ticket de la API** en <https://www.chilecompra.cl/api/> ("Pide tu ticket",
   con Clave Única). Llega por correo. El mismo ticket sirve para Licitaciones y Compra Ágil.
2. **Guardarlo como secreto** del repositorio: *Settings → Secrets and variables → Actions →
   New repository secret*, nombre `MERCADO_PUBLICO_TICKET`.
   No lo pegues en el código ni en issues.
3. **Activar GitHub Pages**: *Settings → Pages → Build and deployment → Source:
   **GitHub Actions***. (En repos privados Pages requiere plan Team/Pro; si no lo tienes,
   ve la opción local más abajo o haz público el repo.)
4. **Mergear a `main`**: las ejecuciones programadas de GitHub Actions solo corren en la rama
   principal. Luego, en *Actions → Radar de licitaciones → Run workflow*, lánzalo una vez a
   mano para no esperar a la siguiente hora.

El dashboard quedará en `https://<organización>.github.io/<repositorio>/`. Para servirlo bajo
un subdominio propio (p. ej. `licitaciones.aereo.cl`), configúralo en *Settings → Pages →
Custom domain* y crea el registro CNAME en el DNS de aereo.cl.

> GitHub puede retrasar unos minutos las ejecuciones programadas. Los commits horarios del
> radar mantienen el repositorio activo, así que el cron no se desactiva por inactividad.

## Diseño y acceso

- El dashboard usa el sistema de diseño de aereo.cl (repo `aereo-web`: `partials/chrome.css` y
  `assets/aereo-ui.css`): amarillo `#ECE31D`, Big Shoulders Display + Roboto + Roboto Mono
  (copiadas en `docs/assets/fonts/`) y sombras duras negras.
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
radar/fuentes.py            clientes de las APIs (Licitaciones v1, Compra Ágil v2)
radar/monitor.py            orquestación, fusión con datos previos, JSON de salida
docs/index.html             dashboard
docs/data/licitaciones.json datos publicados (los escribe el radar)
data/cache.json             estado interno (qué ya se revisó)
tests/                      tests con respuestas simuladas de las APIs
```
