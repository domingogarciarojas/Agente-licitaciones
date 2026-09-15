# Agente de licitaciones — Consultoría y Formación (PLACSP)

Cada semana descarga el feed de la **Plataforma de Contratación del Sector Público**,
filtra las licitaciones de **consultoría** y **formación** de los últimos 7 días y te
manda un email con una tabla: **título · importe de licitación · descripción · criterios de adjudicación**.

## Cómo funciona
- Fuente: feed ATOM de datos abiertos de la PLACSP (excluye contratos menores).
- Filtro: por código **CPV** (79x consultoría, 80x formación) + refuerzo por palabras clave.
- Envío: email HTML por SMTP.

## Puesta en marcha con GitHub Actions (recomendado, gratis)

1. Crea un repositorio nuevo en GitHub y sube estos archivos.
2. Ve a **Settings → Secrets and variables → Actions → New repository secret** y crea:
   - `SMTP_HOST`  → p.ej. `smtp.gmail.com`
   - `SMTP_PORT`  → `587`
   - `SMTP_USER`  → tu email
   - `SMTP_PASS`  → **contraseña de aplicación** (en Gmail: cuenta con 2FA → Contraseñas de aplicaciones). NO tu contraseña normal.
   - `EMAIL_TO`   → destinatario (puede ser el mismo). Varios separados por comas.
   - `EMAIL_FROM` → remitente (normalmente igual que SMTP_USER).
3. Listo. Corre solo cada **lunes a las 07:00 UTC**. Para probarlo ya:
   pestaña **Actions → Agente licitaciones semanal → Run workflow**.

## Ejecución local (alternativa)

```bash
pip install -r requirements.txt
export SMTP_USER="tucorreo@gmail.com"
export SMTP_PASS="contraseña_de_aplicacion"
export EMAIL_TO="tucorreo@gmail.com"
python agente_licitaciones.py
```

Si no defines credenciales SMTP, el informe se imprime por pantalla (útil para probar el filtro).

## Ajustes (variables de entorno opcionales)

| Variable        | Por defecto            | Para qué |
|-----------------|------------------------|----------|
| `DIAS_VENTANA`  | `7`                    | Días hacia atrás que se revisan |
| `CPV_PREFIJOS`  | `794,7941,7942,805,8051,8053` | Prefijos CPV que interesan |
| `KEYWORDS`      | consultor, formación…  | Refuerzo por texto si el CPV falta |
| `IMPORTE_MAX`   | `0` (sin límite)       | Descarta licitaciones por encima de X € |
| `MAX_PAGINAS`   | `12`                   | Cuántas páginas del feed recorrer |
| `PLACSP_FEED_URL` | feed oficial         | Cambiar de feed si hace falta |

## Notas importantes
- **Verifica la URL del feed** la primera vez (ver más abajo): la PLACSP reorganizó
  sus servicios de sindicación y las rutas cambian de vez en cuando. Si el script
  no trae nada y en el log ves un WARN de descarga, actualiza `PLACSP_FEED_URL`.
- Los criterios de adjudicación no siempre vienen completos en el feed; para el
  detalle final entra en la ficha (columna Título enlaza a la licitación).
- Esto es un aviso automático: **revisa siempre los pliegos oficiales** antes de presentarte.
