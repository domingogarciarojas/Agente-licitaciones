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
# Códigos CPV que nos interesan (perfil CES Institute: auditorías industriales y
# energéticas, diagnóstico e implantación de normas ISO). Filtramos por PREFIJO.
#   79212  -> servicios de auditoría
#   71314  -> energía y servicios conexos / asesoramiento eficiencia energética
#   71315  -> servicios técnicos de instalaciones de edificios (energía)
#   71356  -> servicios técnicos (ingeniería industrial)
#   71630  -> servicios de inspección y ensayo técnicos
#   71631  -> servicios de inspección técnica (instalaciones)
#   90714  -> auditoría medioambiental
#   79411  -> consultoría de gestión (implantación de sistemas de gestión)
#   73220  -> servicios de consultoría en desarrollo
#   80500,80510,80520,80530  -> servicios de formación (acotados por keyword al perfil)
CPV_PREFIJOS = tuple(
    p.strip() for p in os.environ.get(
        "CPV_PREFIJOS",
        "79212,71314,71315,71356,71630,71631,90714,79411,73220,80500,80510,80520,80530",
    ).split(",")
)

# Palabras clave de refuerzo (por si el CPV no viene bien informado).
# Palabras clave de refuerzo (perfil CES Institute). Si el CPV no viene bien
# informado, basta con que aparezca UNA de estas en título o descripción.
KEYWORDS = tuple(
    k.strip().lower()
    for k in os.environ.get(
        "KEYWORDS",
        "auditoría energética,auditoria energetica,eficiencia energética,eficiencia energetica,"
        "iso 9001,iso 14001,iso 45001,iso 50001,iso 27001,iso 17025,"
        "sistema de gestión,sistema de gestion,certificación iso,certificacion iso,"
        "implantación de la norma,implantacion de la norma,auditoría industrial,auditoria industrial,"
        "seguridad industrial,gestión energética,gestion energetica,diagnóstico energético,"
        "diagnostico energetico,servicios de auditoría,servicios de auditoria,"
        "inspección reglamentaria,inspeccion reglamentaria,inspección periódica,inspeccion periodica,"
        "organismo de control,baja tensión,baja tension,alta tensión,alta tension,"
        "instalación térmica,instalacion termica,rite,instalación frigorífica,instalacion frigorifica,"
        "equipos a presión,equipos a presion,almacenamiento de productos químicos,apq,"
        "instalación de gas,instalacion de gas,legalización de instalaciones,legalizacion de instalaciones,"
        "cumplimiento reglamentario,seguridad industrial,"
        "formación en prevención,formacion en prevencion,curso de eficiencia energética,"
        "curso de eficiencia energetica,formación iso,formacion iso,curso iso,"
        "formación en seguridad industrial,formacion en seguridad industrial,"
        "curso de auditor,formación de auditores,formacion de auditores,"
        "formación energética,formacion energetica,curso de gestión energética,"
        "curso de gestion energetica,capacitación técnica industrial,capacitacion tecnica industrial",
    ).split(",")
)

# Palabras que DESCARTAN una licitación aunque coincida en CPV o keyword.
# Sirve para quitar el ruido típico (obras, limpieza, formación genérica, TI...).
EXCLUIR = tuple(
    e.strip().lower()
    for e in os.environ.get(
        "EXCLUIR",
        "obras,obra civil,limpieza,jardinería,jardineria,catering,seguridad privada,vigilancia,"
        "suministro de,mobiliario,software,desarrollo de aplicaciones,mantenimiento informático,"
        "mantenimiento informatico,auditoría de cuentas,auditoria de cuentas,auditoría contable,"
        "auditoria contable,seguros,póliza,poliza,combustible,vehículos,vehiculos",
    ).split(",")
    if e.strip()
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


def esta_excluida(titulo, descripcion):
    blob = f"{titulo} {descripcion}".lower()
    return any(x in blob for x in EXCLUIR)


def es_cpv_formacion(cpvs):
    return any(c.startswith("805") for c in cpvs)


# Materias propias de CES: la formación solo interesa si toca una de estas.
MATERIAS_CES = (
    "iso", "energ", "auditor", "eficiencia", "seguridad industrial", "instalacion",
    "instalación", "prevención", "prevencion", "reglament", "gestión energética",
    "gestion energetica", "medioambient", "calidad", "45001", "14001", "9001",
    "50001", "27001", "riesgos laborales", "baja tensión", "baja tension",
    "alta tensión", "alta tension", "gas", "frigorific", "térmica", "termica",
    "equipos a presión", "equipos a presion", "apq",
)


def toca_materia_ces(titulo, descripcion):
    blob = f"{titulo} {descripcion}".lower()
    return any(m in blob for m in MATERIAS_CES)


def interesa(lic):
    # 1) Debe encajar por CPV o por palabra clave.
    if not (coincide_cpv(lic["cpvs"]) or coincide_keyword(lic["titulo"], lic["descripcion"])):
        return False
    # 2) Se descarta si contiene una palabra de la lista de exclusión.
    if esta_excluida(lic["titulo"], lic["descripcion"]):
        return False
    # 3) Si es formación genérica (CPV 805xx), solo pasa si toca una materia de CES.
    #    Así evitamos formación de idiomas, ofimática, etc. que no os interesa.
    if es_cpv_formacion(lic["cpvs"]) and not toca_materia_ces(lic["titulo"], lic["descripcion"]):
        return False
    # 4) Respeta el importe máximo si se ha configurado.
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

