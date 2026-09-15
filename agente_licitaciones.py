#!/usr/bin/env python3
"""
Agente de licitaciones - Plataforma de Contratación del Sector Público (PLACSP)
==============================================================================
Descarga el feed ATOM de licitaciones (excluyendo contratos menores), filtra
las de CONSULTORÍA y FORMACIÓN publicadas en la última semana, y envía por
email una tabla resumen con: título, importe de licitación, descripción y
criterios de adjudicación.

Diseñado para ejecutarse 1 vez/semana (GitHub Actions o cron).

Autor: generado con Claude
"""

import os
import sys
import smtplib
import datetime as dt
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests
from lxml import etree

# ---------------------------------------------------------------------------
# CONFIGURACIÓN
# ---------------------------------------------------------------------------

# Feeds ATOM (sin contratos menores). Cada fichero trae máx. 500 entradas y se
# pagina con el enlace rel="next".
#   - Perfiles del contratante alojados en la PLACSP.
#   - Agregación: licitaciones que llegan desde plataformas autonómicas.
# Por defecto se consultan ambos; para usar solo uno, ajusta PLACSP_FEEDS.
FEEDS_DEFECTO = ",".join([
    "https://contrataciondelestado.es/sindicacion/sindicacion_643/licitacionesPerfilesContratanteCompleto3.atom",
    "https://contrataciondelestado.es/sindicacion/sindicacion_1044/licitacionesPerfilesContratanteCompleto3.atom",
])
FEED_URLS = [u.strip() for u in os.environ.get("PLACSP_FEEDS", FEEDS_DEFECTO).split(",") if u.strip()]

# El servidor de la PLACSP a veces presenta un certificado SSL problemático.
# Si ves errores de SSL, pon VERIFY_SSL=0 (equivale al -k de curl).
VERIFY_SSL = os.environ.get("VERIFY_SSL", "1") != "0"

# Nº de días hacia atrás que consideramos "la última semana".
DIAS_VENTANA = int(os.environ.get("DIAS_VENTANA", "7"))

# Nº máximo de páginas del feed a recorrer (500 entradas cada una).
# Sube este número si publicas muchas entidades o amplías la ventana temporal.
MAX_PAGINAS = int(os.environ.get("MAX_PAGINAS", "12"))

# Códigos CPV que nos interesan. Filtramos por PREFIJO, así que basta la raíz.
#   79xxxxxx  -> servicios empresariales, de consultoría y gestión
#   80xxxxxx  -> servicios de enseñanza y formación
# Se puede afinar más (p.ej. 79400000, 80500000) para reducir ruido.
CPV_PREFIJOS = tuple(
    p.strip() for p in os.environ.get("CPV_PREFIJOS", "794,7941,7942,805,8051,8053").split(",")
)

# Palabras clave de refuerzo (por si el CPV no viene bien informado).
KEYWORDS = tuple(
    k.strip().lower()
    for k in os.environ.get(
        "KEYWORDS",
        "consultor,consultoría,asistencia técnica,formación,formativo,capacitación,docencia,curso",
    ).split(",")
)

# Importe máximo al que os presentáis (EUR). Por encima se descarta. 0 = sin límite.
IMPORTE_MAX = float(os.environ.get("IMPORTE_MAX", "0"))

# --- Email (se leen de variables de entorno / secrets, nunca en el código) ---
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")  # contraseña de aplicación, NO la normal
EMAIL_TO = os.environ.get("EMAIL_TO", SMTP_USER)
EMAIL_FROM = os.environ.get("EMAIL_FROM", SMTP_USER)

# Namespaces del CODICE-XML embebido en el ATOM.
NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "cbc": "urn:dgpe:names:draft:codice:schema:xsd:CommonBasicComponents-2",
    "cac": "urn:dgpe:names:draft:codice:schema:xsd:CommonAggregateComponents-2",
    "cbc-place": "urn:dgpe:names:draft:codice-place-ext:schema:xsd:CommonBasicComponents-2",
    "cac-place": "urn:dgpe:names:draft:codice-place-ext:schema:xsd:CommonAggregateComponents-2",
}


# ---------------------------------------------------------------------------
# EXTRACCIÓN DEL FEED
# ---------------------------------------------------------------------------

def descargar_feed(url):
    """Descarga una página del feed ATOM. Devuelve el árbol lxml o None."""
    try:
        if not VERIFY_SSL:
            import urllib3
            urllib3.disable_warnings()
        r = requests.get(
            url,
            timeout=60,
            headers={"User-Agent": "agente-licitaciones/1.0"},
            verify=VERIFY_SSL,
        )
        r.raise_for_status()
        return etree.fromstring(r.content)
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] No se pudo descargar {url}: {e}", file=sys.stderr)
        return None


def texto(nodo, xpath):
    """Devuelve el texto del primer match del xpath, o '' si no hay."""
    try:
        res = nodo.xpath(xpath, namespaces=NS)
        if res:
            val = res[0]
            return (val.text if hasattr(val, "text") else str(val)) or ""
    except Exception:  # noqa: BLE001
        pass
    return ""


def parsear_entrada(entry):
    """Extrae los campos que nos interesan de una <entry> del ATOM."""
    # El CODICE va dentro de cac:ProcurementProjectLot / ProcurementProject, etc.
    titulo = texto(entry, ".//cac:ProcurementProject/cbc:Name") or texto(entry, "atom:title")
    descripcion = texto(entry, ".//cac:ProcurementProject/cbc:Description")

    # Importe de licitación (presupuesto base sin impuestos).
    importe = texto(
        entry,
        ".//cac:ProcurementProject/cac:BudgetAmount/cbc:TotalAmount",
    )
    if not importe:
        importe = texto(
            entry, ".//cac:BudgetAmount/cbc:EstimatedOverallContractAmount"
        )

    # CPV: puede haber varios.
    cpvs = entry.xpath(
        ".//cac:ProcurementProject//cbc:ItemClassificationCode/text()", namespaces=NS
    )

    # Criterios de adjudicación.
    criterios = entry.xpath(
        ".//cac:AwardingCriteria/cbc:Description/text()", namespaces=NS
    )
    if not criterios:
        criterios = entry.xpath(
            ".//cac:AwardingTerms//cbc:Description/text()", namespaces=NS
        )

    # Enlace a la ficha pública.
    enlace = texto(entry, "atom:link/@href") or texto(entry, "atom:id")

    # Fecha de actualización.
    actualizado = texto(entry, "atom:updated")

    return {
        "titulo": (titulo or "").strip(),
        "descripcion": (descripcion or "").strip(),
        "importe": (importe or "").strip(),
        "cpvs": [c.strip() for c in cpvs],
        "criterios": " / ".join(c.strip() for c in criterios if c.strip()),
        "enlace": (enlace or "").strip(),
        "actualizado": (actualizado or "").strip(),
    }


# ---------------------------------------------------------------------------
# FILTRADO
# ---------------------------------------------------------------------------

def es_reciente(actualizado_iso, limite):
    if not actualizado_iso:
        return True  # si no hay fecha, no lo descartamos por fecha
    try:
        fecha = dt.datetime.fromisoformat(actualizado_iso.replace("Z", "+00:00"))
        return fecha.date() >= limite
    except Exception:  # noqa: BLE001
        return True


def coincide_cpv(cpvs):
    for c in cpvs:
        if any(c.startswith(pref) for pref in CPV_PREFIJOS):
            return True
    return False


def coincide_keyword(titulo, descripcion):
    blob = f"{titulo} {descripcion}".lower()
    return any(k in blob for k in KEYWORDS)


def importe_ok(importe_str):
    if IMPORTE_MAX <= 0:
        return True
    try:
        val = float(importe_str.replace(".", "").replace(",", "."))
        return val <= IMPORTE_MAX
    except Exception:  # noqa: BLE001
        return True  # si no se puede parsear, no descartamos


def interesa(lic):
    if not (coincide_cpv(lic["cpvs"]) or coincide_keyword(lic["titulo"], lic["descripcion"])):
        return False
    if not importe_ok(lic["importe"]):
        return False
    return True


# ---------------------------------------------------------------------------
# RECOLECCIÓN COMPLETA
# ---------------------------------------------------------------------------

def recolectar():
    limite = (dt.datetime.now().date() - dt.timedelta(days=DIAS_VENTANA))
    resultados = []
    vistos = set()

    for feed_inicial in FEED_URLS:
        url = feed_inicial
        _recorrer_feed(url, limite, resultados, vistos)

    return resultados


def _recorrer_feed(url, limite, resultados, vistos):
    for pagina in range(MAX_PAGINAS):
        arbol = descargar_feed(url)
        if arbol is None:
            break

        entradas = arbol.xpath("//atom:entry", namespaces=NS)
        if not entradas:
            break

        pagina_tiene_reciente = False
        for entry in entradas:
            lic = parsear_entrada(entry)
            if es_reciente(lic["actualizado"], limite):
                pagina_tiene_reciente = True
                clave = lic["enlace"] or lic["titulo"]
                if clave in vistos:
                    continue
                if interesa(lic):
                    vistos.add(clave)
                    resultados.append(lic)

        # Enlace a la página siguiente.
        siguiente = arbol.xpath("//atom:link[@rel='next']/@href", namespaces=NS)

        # Si esta página ya no trae nada de la última semana, dejamos de paginar
        # (el feed viene ordenado de más nuevo a más antiguo).
        if not pagina_tiene_reciente:
            break
        if not siguiente:
            break
        url = siguiente[0]


# ---------------------------------------------------------------------------
# INFORME + EMAIL
# ---------------------------------------------------------------------------

def formatear_importe(imp):
    if not imp:
        return "—"
    try:
        val = float(imp.replace(".", "").replace(",", "."))
        return f"{val:,.0f} €".replace(",", ".")
    except Exception:  # noqa: BLE001
        return imp


def construir_html(resultados):
    hoy = dt.date.today().strftime("%d/%m/%Y")
    if not resultados:
        return f"""
        <p>Informe semanal de licitaciones — {hoy}</p>
        <p>No se han encontrado licitaciones de consultoría o formación en los últimos
        {DIAS_VENTANA} días que encajen con los criterios configurados.</p>
        """

    filas = ""
    for lic in resultados:
        filas += f"""
        <tr>
          <td style="padding:8px;border:1px solid #ddd;vertical-align:top;">
            <a href="{lic['enlace']}">{lic['titulo'] or '(sin título)'}</a>
          </td>
          <td style="padding:8px;border:1px solid #ddd;vertical-align:top;white-space:nowrap;">
            {formatear_importe(lic['importe'])}
          </td>
          <td style="padding:8px;border:1px solid #ddd;vertical-align:top;">
            {lic['descripcion'] or '—'}
          </td>
          <td style="padding:8px;border:1px solid #ddd;vertical-align:top;">
            {lic['criterios'] or '—'}
          </td>
        </tr>"""

    return f"""
    <p><strong>Informe semanal de licitaciones — {hoy}</strong></p>
    <p>{len(resultados)} licitación(es) de consultoría / formación en los últimos
    {DIAS_VENTANA} días:</p>
    <table style="border-collapse:collapse;font-family:Arial,sans-serif;font-size:13px;width:100%;">
      <thead>
        <tr style="background:#f2f2f2;">
          <th style="padding:8px;border:1px solid #ddd;text-align:left;">Título</th>
          <th style="padding:8px;border:1px solid #ddd;text-align:left;">Importe licitación</th>
          <th style="padding:8px;border:1px solid #ddd;text-align:left;">Descripción</th>
          <th style="padding:8px;border:1px solid #ddd;text-align:left;">Criterios de adjudicación</th>
        </tr>
      </thead>
      <tbody>{filas}</tbody>
    </table>
    <p style="color:#888;font-size:11px;">Fuente: Plataforma de Contratación del Sector Público
    (datos abiertos). Verifica siempre los pliegos oficiales antes de presentarte.</p>
    """


def enviar_email(html):
    if not (SMTP_USER and SMTP_PASS):
        print("[ERROR] Faltan credenciales SMTP_USER / SMTP_PASS.", file=sys.stderr)
        # Aun así imprimimos el informe por consola para no perderlo.
        print(html)
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Licitaciones consultoría/formación — {dt.date.today():%d/%m/%Y}"
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(EMAIL_FROM, EMAIL_TO.split(","), msg.as_string())
        print(f"[OK] Email enviado a {EMAIL_TO}.")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] No se pudo enviar el email: {e}", file=sys.stderr)
        print(html)
        return False


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    print(f"[INFO] Ventana: últimos {DIAS_VENTANA} días | CPV: {CPV_PREFIJOS}")
    resultados = recolectar()
    print(f"[INFO] {len(resultados)} licitaciones encontradas.")
    html = construir_html(resultados)
    enviar_email(html)


if __name__ == "__main__":
    main()
