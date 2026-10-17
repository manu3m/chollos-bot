#!/usr/bin/env python3
"""
Bot de alertas de chollos de Amazon -> Telegram, con filtro por categorías.

Lee las ofertas nuevas de Chollometro, se queda con las de Amazon y avisa:
  - de las que superen tu umbral (80 % por defecto) en las categorías que elijas;
  - de los "chollazos" (95 % o más) de cualquier categoría, si los tienes activados;
  - de las apps de iOS que se pongan gratis (100 %) en la App Store, si lo activas.
Las categorías y el umbral se cambian desde Telegram con /categorias y /umbral.

Funciona en GitHub Actions como un bucle de varias horas: responde al instante en
Telegram, revisa las ofertas cada pocos minutos y, antes de terminar, se relanza solo.
"""
import html
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

# ----------------------------- Configuración -----------------------------
TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
DESCUENTO_MIN = float(os.environ.get("DESCUENTO_MIN") or 80)
UMBRAL_CHOLLAZOS = float(os.environ.get("UMBRAL_CHOLLAZOS") or 95)
MODO = (os.environ.get("MODO") or "normal").strip().lower()
TIENDA = (os.environ.get("TIENDA") or "amazon").strip().lower()
FEEDS = [f.strip() for f in (os.environ.get("FEEDS") or
         "https://www.chollometro.com/rss/nuevos,https://www.chollometro.com/rss"
         ).split(",") if f.strip()]
_excluir = (os.environ.get("EXCLUIR") or "kindle,ebook,e-book").strip()
EXCLUIR = [] if _excluir.lower() == "ninguno" else [
    p.strip().lower() for p in _excluir.split(",") if p.strip()]

# (nombre corto para los botones, nombre exacto de la categoría en Chollometro)
CATEGORIAS = [
    ("Electrónica", "Electrónica"),
    ("Gaming", "Gaming"),
    ("Hogar y oficina", "Hogar, vivienda y oficina"),
    ("Jardín y bricolaje", "Jardín y bricolaje"),
    ("Juguetes y familia", "Juguetes, familia e hijos"),
    ("Moda", "Moda y accesorios"),
    ("Salud y belleza", "Salud y belleza"),
    ("Supermercado", "Supermercado y alimentación"),
    ("Deportes", "Deportes y aire libre"),
    ("Cultura y ocio", "Cine, libros, cultura y ocio"),
    ("Coches y motos", "Coches y motos"),
    ("Otras", "Otras"),
]
VERSION = 4
DURACION = int(os.environ.get("DURACION_MIN") or 330) * 60   # cuánto dura cada tanda
INTERVALO = int(os.environ.get("INTERVALO_SEG") or 240)      # cada cuánto mira ofertas

ESTADO = Path(__file__).with_name("vistos.json")
MAX_VISTOS = 3000
MAX_PAGINAS = 15
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")

PALABRAS_ANTES = (r"(?:antes|pvpr?|p\.v\.p\.?r?\.?|precio\s(?:anterior|habitual|"
                  r"original|recomendado|normal)|era|en\svez\sde|en\slugar\sde|"
                  r"sin\sdescuento)")

AYUDA = ("🤖 <b>Comandos</b>\n"
         "/categorias – elegir departamentos, chollazos y apps iOS gratis\n"
         "/umbral 85 – cambiar el descuento mínimo de aviso\n"
         "/estado – ver tu configuración actual\n"
         "/diagnostico – comprobar que todo funciona")

fallo_telegram = False


# ------------------------------ Utilidades -------------------------------
def descargar(url, timeout=20):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept-Language": "es-ES,es;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def limpiar(texto):
    texto = re.sub(r"<[^>]+>", " ", texto or "")
    return re.sub(r"\s+", " ", html.unescape(texto)).strip()


def normalizar(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


MAPA_CATEGORIAS = {normalizar(feed): feed for _, feed in CATEGORIAS}


def a_numero(s):
    """Convierte '1.299,99' / '12,99' / '1,299.99' / '1.299' en float."""
    if s is None:
        return None
    s = s.strip().replace(" ", "").rstrip(".,")
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", s):
        s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None


def precio_de(s):
    if not s:
        return None
    if "gratis" in s.lower():
        return 0.0
    m = re.search(r"\d[\d.,]*", s)
    return a_numero(m.group()) if m else None


# ------------------------------ Lectura RSS ------------------------------
def leer_feed(url):
    raiz = ET.fromstring(descargar(url))
    ofertas = []
    for item in raiz.iter():
        if item.tag.split("}")[-1].lower() not in ("item", "entry"):
            continue
        o = {"titulo": "", "link": "", "id": "", "texto": "",
             "tienda": "", "precio": None, "categorias": []}
        for hijo in item:
            local = hijo.tag.split("}")[-1].lower()
            valor = (hijo.text or "").strip()
            if local == "title":
                o["titulo"] = limpiar(valor)
            elif local == "link":
                o["link"] = valor or hijo.get("href", "")
            elif local in ("guid", "id"):
                o["id"] = valor
            elif local == "category":
                o["categorias"].append(limpiar(valor or hijo.get("term", "")))
            elif local == "merchant":
                o["tienda"] = hijo.get("name", "") or valor
                o["precio"] = precio_de(hijo.get("price", ""))
            elif local in ("description", "summary", "content", "encoded"):
                o["texto"] += " " + limpiar(valor)
        o["id"] = o["id"] or o["link"]
        if o["id"]:
            ofertas.append(o)
    return ofertas


def categoria_de(o):
    for c in o["categorias"]:
        feed = MAPA_CATEGORIAS.get(normalizar(c))
        if feed:
            return feed
    return "Otras"


def nombre_corto(feed):
    return next((corto for corto, f in CATEGORIAS if f == feed), feed)


# -------------------------- Cálculo del descuento ------------------------
def calcular_descuento(o):
    texto = f"{o['titulo']} {o['texto']}"
    candidatos = []

    patron = (r"-\s?(\d{1,2}(?:[.,]\d)?)\s?%|(\d{1,2}(?:[.,]\d)?)\s?%\s?"
              r"(?:de\s)?(?:dto|descuento|rebaja|off)")
    for m in re.finditer(patron, texto, re.I):
        v = a_numero(m.group(1) or m.group(2))
        if v and 0 < v < 100:
            candidatos.append(v)

    actual = o["precio"]
    if actual is None:
        if re.search(r"\bgratis\b", o["titulo"], re.I):
            actual = 0.0
        else:
            precios = [a_numero(p) for p in re.findall(r"(\d[\d.,]*)\s?€", o["titulo"])]
            precios = [p for p in precios if p is not None]
            if precios:
                actual = min(precios)
    if actual is not None:
        for m in re.finditer(PALABRAS_ANTES + r"[:\s]*(\d[\d.,]*)\s?€", texto, re.I):
            antes = a_numero(m.group(1))
            if antes and antes > actual:
                candidatos.append(round((1 - actual / antes) * 100, 1))

    return (max(candidatos) if candidatos else None), actual


def precios_pagina(o):
    """Abre la página de la oferta y devuelve (precio actual, precio anterior)."""
    try:
        pag = html.unescape(descargar(o["link"]))
    except Exception:
        return None, None
    m_act = re.search(r'"price"\s*:\s*"?(\d+(?:\.\d+)?)', pag)
    m_ant = re.search(r'"nextBestPrice"\s*:\s*"?(\d+(?:\.\d+)?)', pag)
    return (float(m_act.group(1)) if m_act else None,
            float(m_ant.group(1)) if m_ant else None)


def descuento_desde_pagina(o):
    """Plan B: calcula el descuento con los precios de la página de la oferta."""
    actual, antes = precios_pagina(o)
    if actual is not None and antes and antes > actual:
        return round((1 - actual / antes) * 100, 1), actual
    return None, actual


def analizar(lista):
    resultados, paginas = [], 0
    for o in lista:
        desc, actual = calcular_descuento(o)
        if desc is None and paginas < MAX_PAGINAS:
            paginas += 1
            desc, act2 = descuento_desde_pagina(o)
            actual = actual if actual is not None else act2
        resultados.append((o, desc, actual))
    return resultados


# --------------------------- Enlace directo -------------------------------
class _SinRedirecciones(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_abridor = urllib.request.build_opener(_SinRedirecciones)


def es_tienda_final(url):
    host = urllib.parse.urlparse(url).netloc.lower()
    return host.startswith("apps.apple.com") or (
        "amazon." in host and "amzn.to" not in host)


def limpiar_destino(url):
    """Deja el enlace de Amazon en su forma corta /dp/ASIN y quita parámetros a la App Store."""
    u = urllib.parse.urlparse(url)
    if "amazon." in u.netloc:
        m = re.search(r"/(?:dp|gp/product|gp/aw/d|product)/([A-Z0-9]{10})", u.path)
        if m:
            return f"https://{u.netloc}/dp/{m.group(1)}"
    if u.netloc.startswith("apps.apple.com"):
        return f"https://apps.apple.com{u.path}"
    return url


def enlace_visita(o):
    m = re.search(r"(\d{4,})/?$", o["link"]) or re.search(r"(\d{4,})", o["id"])
    if m and "chollometro" in o["link"]:
        return f"https://www.chollometro.com/visit/threadmain/{m.group(1)}"
    return None


def resolver(url, saltos=6):
    """Sigue las redirecciones sin llegar a abrir la tienda; devuelve la URL de Amazon/App Store."""
    for _ in range(saltos):
        if es_tienda_final(url):
            return limpiar_destino(url)
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "es-ES,es;q=0.9"})
        try:
            with _abridor.open(req, timeout=15) as r:
                cuerpo = r.read(60000).decode("utf-8", errors="replace")
            m = (re.search(r'http-equiv=["\']refresh["\'][^>]*url=([^"\'>]+)', cuerpo, re.I)
                 or re.search(r'location(?:\.href)?\s*=\s*["\']([^"\']+)', cuerpo))
            if not m:
                return None
            url = urllib.parse.urljoin(url, html.unescape(m.group(1)))
        except urllib.error.HTTPError as e:
            destino = e.headers.get("Location")
            if e.code in (301, 302, 303, 307, 308) and destino:
                url = urllib.parse.urljoin(url, destino)
            else:
                return None
        except Exception:
            return None
    return None


def enlaces(o):
    """(enlace directo a la tienda, enlace a Chollometro)."""
    visita = enlace_visita(o)
    directo = resolver(visita) if visita else None
    return directo or visita or o["link"], o["link"]


def lineas_enlace(o, texto_tienda):
    e = html.escape
    directo, chollometro = enlaces(o)
    lineas = [f'👉 <a href="{e(directo, quote=True)}"><b>{texto_tienda}</b></a>']
    if directo != chollometro:
        lineas.append(f'💬 <a href="{e(chollometro, quote=True)}">Ver en Chollometro</a>')
    return lineas


# ------------------------------- Apps iOS --------------------------------
def es_app_ios(o):
    if o["tienda"]:
        return "app store" in o["tienda"].lower()
    texto = f"{o['titulo']} {o['texto']}".lower()
    return bool(re.search(r"\bios\b|iphone|ipad", o["titulo"], re.I)) and "app store" in texto


def es_gratis(o):
    if o["precio"] == 0:
        return True
    return bool(re.search(r"\bgratis\b|-\s?100\s?%|\bfree\b", o["titulo"], re.I))


def precio_anterior(o):
    for m in re.finditer(PALABRAS_ANTES + r"[:\s]*(\d[\d.,]*)\s?€", f"{o['titulo']} {o['texto']}", re.I):
        v = a_numero(m.group(1))
        if v:
            return v
    return precios_pagina(o)[1]


def formatear_app(o, antes):
    e = html.escape
    lineas = ["📱 <b>App de iOS GRATIS (-100%)</b>", f"<b>{e(o['titulo'])}</b>"]
    if antes:
        lineas.append(f"💶 Antes {euros(antes)} → ahora GRATIS")
    lineas += lineas_enlace(o, "Abrir en la App Store")
    lineas.append("<i>Las apps gratis suelen durar poco: si te interesa, descárgala ya y queda en tu cuenta para siempre.</i>")
    return "\n".join(lineas)


# ------------------------------- Telegram --------------------------------
def tg(metodo, **params):
    if not TOKEN:
        print(f"[Telegram no configurado] {metodo}: {params.get('text', '')}\n")
        return None
    datos = {k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
             for k, v in params.items()}
    req = urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/{metodo}",
                                 data=urllib.parse.urlencode(datos).encode())
    try:
        espera = int(params.get("timeout", 0)) + 20
        with urllib.request.urlopen(req, timeout=espera) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        print("Telegram", metodo, e.code, e.read().decode(errors="replace"))
    except Exception as e:
        print("Telegram", metodo, e)
    return None


def telegram(texto, **extra):
    global fallo_telegram
    r = tg("sendMessage", chat_id=CHAT_ID, text=texto, parse_mode="HTML",
           disable_web_page_preview="false", **extra)
    if TOKEN and not r:
        fallo_telegram = True
    time.sleep(1.1)  # Telegram limita a ~1 mensaje por segundo
    return r


def euros(v):
    return "GRATIS" if v == 0 else f"{v:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


def formatear(o, desc, actual, categoria, chollazo=False):
    e = html.escape
    cabecera = (f"💥 <b>¡CHOLLAZO! -{desc:.0f}%</b> en Amazon" if chollazo
                else f"🔥 <b>-{desc:.0f}%</b> en Amazon")
    lineas = [cabecera, f"<b>{e(o['titulo'])}</b>", f"🏷️ {e(nombre_corto(categoria))}"]
    if actual is not None:
        lineas.append(f"💶 {euros(actual)}")
    lineas += lineas_enlace(o, "Abrir en Amazon")
    return "\n".join(lineas)


# ------------------------- Preferencias y menú ---------------------------
def preferencias(estado):
    p = estado.setdefault("prefs", {})
    cats = p.setdefault("categorias", {})
    for _, feed in CATEGORIAS:
        cats.setdefault(feed, True)
    p.setdefault("chollazos", True)
    p.setdefault("apps_ios", True)
    p.setdefault("umbral", DESCUENTO_MIN)
    return p


def texto_menu(p):
    return ("⚙️ <b>Filtros de ofertas</b>\n"
            "Toca un departamento para activarlo ✅ o desactivarlo ⬜.\n\n"
            f"🔥 <b>Avisos normales:</b> descuento del {p['umbral']:.0f}% o más, "
            "solo en los departamentos marcados.\n"
            f"💥 <b>Chollazos:</b> descuento del {UMBRAL_CHOLLAZOS:.0f}% o más, "
            "de cualquier departamento (aunque esté desmarcado).\n"
            "📱 <b>Apps iOS gratis:</b> apps de la App Store que pasan a costar 0 €.\n\n"
            "<i>Los cambios se aplican al momento.</i>")


def teclado(p):
    filas, fila = [], []
    for i, (corto, feed) in enumerate(CATEGORIAS):
        marca = "✅" if p["categorias"][feed] else "⬜"
        fila.append({"text": f"{marca} {corto}", "callback_data": f"c:{i}"})
        if len(fila) == 2:
            filas.append(fila)
            fila = []
    if fila:
        filas.append(fila)
    marca = "✅" if p["chollazos"] else "⬜"
    filas.append([{"text": f"{marca} 💥 Chollazos ≥{UMBRAL_CHOLLAZOS:.0f}%",
                   "callback_data": "x"}])
    marca = "✅" if p["apps_ios"] else "⬜"
    filas.append([{"text": f"{marca} 📱 Apps iOS gratis (100%)", "callback_data": "i"}])
    filas.append([{"text": "Marcar todos", "callback_data": "all"},
                  {"text": "Desmarcar todos", "callback_data": "none"}])
    return {"inline_keyboard": filas}


def enviar_menu(p):
    telegram(texto_menu(p), reply_markup=teclado(p))


def resumen(p):
    activas = [c for c, f in CATEGORIAS if p["categorias"][f]]
    return ("📋 <b>Tu configuración</b>\n"
            f"Umbral de aviso: {p['umbral']:.0f}%\n"
            f"Chollazos (≥{UMBRAL_CHOLLAZOS:.0f}%): {'activados' if p['chollazos'] else 'desactivados'}\n"
            f"Apps iOS gratis: {'activadas' if p['apps_ios'] else 'desactivadas'}\n"
            f"Departamentos: {', '.join(activas) if activas else 'ninguno'}")


def procesar_telegram(estado, espera=0):
    """Espera hasta "espera" segundos a que escribas o pulses algo y lo atiende al momento.
    Devuelve True si ha cambiado algo que haya que guardar."""
    p = preferencias(estado)
    if not TOKEN or not CHAT_ID:
        time.sleep(espera)
        return False
    offset = estado.get("tg_offset", 0)
    r = tg("getUpdates", offset=offset, timeout=espera,
           allowed_updates=["message", "callback_query"])
    if not r or not r.get("ok"):
        time.sleep(5)
        return False
    if not r["result"]:
        return False
    menus = set()
    for u in r["result"]:
        offset = max(offset, u["update_id"] + 1)
        if "callback_query" in u:
            cq = u["callback_query"]
            msg = cq.get("message") or {}
            if str(msg.get("chat", {}).get("id")) != CHAT_ID:
                continue
            d = cq.get("data", "")
            if d.startswith("c:") and d[2:].isdigit() and int(d[2:]) < len(CATEGORIAS):
                feed = CATEGORIAS[int(d[2:])][1]
                p["categorias"][feed] = not p["categorias"][feed]
            elif d == "x":
                p["chollazos"] = not p["chollazos"]
            elif d == "i":
                p["apps_ios"] = not p["apps_ios"]
            elif d in ("all", "none"):
                for _, feed in CATEGORIAS:
                    p["categorias"][feed] = d == "all"
            menus.add(msg.get("message_id"))
            tg("answerCallbackQuery", callback_query_id=cq["id"], text="✅ Cambiado")
        elif "message" in u:
            m = u["message"]
            if str(m.get("chat", {}).get("id")) != CHAT_ID:
                continue
            texto = (m.get("text") or "").strip()
            cmd = texto.split()[0].lower().split("@")[0] if texto else ""
            if cmd in ("/start", "/categorias", "/menu", "/filtros"):
                enviar_menu(p)
            elif cmd == "/umbral":
                partes = texto.split()
                v = a_numero(partes[1].replace("%", "")) if len(partes) > 1 else None
                if v and 1 <= v <= 99:
                    p["umbral"] = v
                    telegram(f"✅ Hecho: te avisaré desde el {v:.0f}% de descuento.")
                else:
                    telegram("Escríbelo así, por ejemplo: /umbral 85")
            elif cmd == "/estado":
                telegram(resumen(p))
            elif cmd == "/diagnostico":
                diagnostico(estado)
            elif texto:
                telegram(AYUDA)
    for mid in menus:
        if mid:
            tg("editMessageText", chat_id=CHAT_ID, message_id=mid, text=texto_menu(p),
               parse_mode="HTML", reply_markup=teclado(p))
    estado["tg_offset"] = offset
    return True


NOVEDADES = {
    2: ("🆕 <b>Novedad: filtro por departamentos</b>\n"
        "Ahora puedes elegir de qué departamentos quieres avisos, y recibir "
        f"siempre los chollazos de más del {UMBRAL_CHOLLAZOS:.0f}%."),
    3: ("🆕 <b>Novedad: apps de iOS gratis</b>\n"
        "Te avisaré cuando una app de pago de la App Store se ponga gratis (-100%). "
        "Puedes desactivarlo en el menú con el botón 📱."),
    4: ("🆕 <b>Ahora respondo al instante</b>\n"
        "Los botones del menú y los comandos funcionan en el momento. "
        "Los botones que pulsaste antes no se han aplicado: usa este menú nuevo."),
}


def presentar_version(estado):
    previa = estado.get("version", 1)
    if previa >= VERSION or not TOKEN or not CHAT_ID:
        return
    if previa < 4:
        # descarta las pulsaciones que se quedaron sin atender con la versión anterior
        r = tg("getUpdates", offset=-1, timeout=0)
        if r and r.get("ok") and r["result"]:
            estado["tg_offset"] = r["result"][-1]["update_id"] + 1
    tg("setMyCommands", commands=[
        {"command": "categorias", "description": "Elegir departamentos, chollazos y apps iOS"},
        {"command": "umbral", "description": "Cambiar el descuento mínimo (ej. /umbral 85)"},
        {"command": "estado", "description": "Ver tu configuración"},
        {"command": "diagnostico", "description": "Comprobar que todo funciona"},
    ])
    textos = [t for v, t in sorted(NOVEDADES.items()) if previa < v <= VERSION]
    telegram("\n\n".join(textos) + "\n\nEscribe /categorias cuando quieras cambiar tus filtros.")
    enviar_menu(preferencias(estado))
    estado["version"] = VERSION


# -------------------------------- Estado ---------------------------------
def cargar_estado():
    try:
        return json.loads(ESTADO.read_text(encoding="utf-8"))
    except Exception:
        return {"vistos": [], "fallando": False, "iniciado": False}


def guardar_estado(estado):
    estado["vistos"] = estado.get("vistos", [])[-MAX_VISTOS:]
    ESTADO.write_text(json.dumps(estado, ensure_ascii=False, indent=0), encoding="utf-8")


# --------------------------------- Main ----------------------------------
def es_de_tienda(o):
    base = f"{o['tienda']} {o['titulo']} {o['link']}"
    if not o["tienda"]:
        base += " " + o["texto"]
    return TIENDA in base.lower()


def excluido(o):
    t = o["titulo"].lower()
    return any(p in t for p in EXCLUIR)


def leer_ofertas():
    ofertas, errores = {}, []
    for url in FEEDS:
        try:
            for o in leer_feed(url):
                ofertas.setdefault(o["id"], o)
        except Exception as ex:
            errores.append(f"{url} → {ex}")
            print("Error leyendo", url, "→", ex)
    return list(ofertas.values()), errores


def diagnostico(estado):
    p = preferencias(estado)
    ofertas, errores = leer_ofertas()
    if not ofertas:
        telegram("⚠️ <b>No he podido leer las ofertas.</b>\n"
                 + html.escape("\n".join(errores))[:900])
        return
    de_tienda = [o for o in ofertas if es_de_tienda(o) and not excluido(o)]
    apps = [o for o in ofertas if es_app_ios(o) and es_gratis(o)]
    res = analizar(de_tienda)
    con_desc = sorted([r for r in res if r[1] is not None], key=lambda r: r[1], reverse=True)
    por_cat = {}
    for o in de_tienda:
        c = nombre_corto(categoria_de(o))
        por_cat[c] = por_cat.get(c, 0) + 1
    msg = ["🧪 <b>Diagnóstico del bot</b>",
           f"Ofertas leídas: {len(ofertas)}",
           f"De {TIENDA.capitalize()}: {len(de_tienda)}",
           f"Con descuento detectado: {len(con_desc)}",
           f"Apps iOS gratis ahora: {len(apps)}",
           f"Umbral de aviso: {p['umbral']:.0f}% · Chollazos: {UMBRAL_CHOLLAZOS:.0f}%",
           "Por departamento: " + html.escape(", ".join(
               f"{c} {n}" for c, n in sorted(por_cat.items(), key=lambda x: -x[1])))]
    if de_tienda:
        directo, _ = enlaces(de_tienda[0])
        msg.append("Enlace directo a Amazon: " + ("✅ funciona" if es_tienda_final(directo)
                   else "⚠️ no disponible, se usará la redirección de Chollometro"))
    if errores:
        msg.append("Fuentes con error: " + html.escape("; ".join(errores))[:400])
    if con_desc:
        msg.append("\n<b>Mayores descuentos ahora mismo:</b>")
        for o, d, a in con_desc[:5]:
            msg.append(f'• -{d:.0f}% <a href="{html.escape(o["link"], quote=True)}">'
                       f'{html.escape(o["titulo"][:70])}</a>')
    telegram("\n".join(msg))


def revisar_ofertas(estado):
    p = preferencias(estado)
    vistos = set(estado["vistos"])
    ofertas, errores = leer_ofertas()

    if not ofertas:
        if not estado.get("fallando"):
            telegram("⚠️ <b>No he podido leer las ofertas.</b>\n"
                     + html.escape("\n".join(errores))[:900])
        estado["fallando"] = True
        return
    if estado.get("fallando"):
        telegram("✅ Vuelvo a leer ofertas con normalidad.")
        estado["fallando"] = False

    de_tienda = [o for o in ofertas if es_de_tienda(o) and not excluido(o)]
    apps = [o for o in ofertas if es_app_ios(o) and es_gratis(o)]

    if not estado.get("iniciado"):
        telegram(f"✅ <b>Bot activado</b>\nHe leído {len(ofertas)} ofertas "
                 f"({len(de_tienda)} de {TIENDA.capitalize()}). Te avisaré en cuanto "
                 f"aparezca un descuento del {p['umbral']:.0f}% o más.")
        estado["iniciado"] = True
    else:
        nuevas = [o for o in de_tienda if o["id"] not in vistos]
        if not p["chollazos"]:
            nuevas = [o for o in nuevas if p["categorias"].get(categoria_de(o), True)]
        for o, d, a in analizar(nuevas):
            if d is None:
                continue
            cat = categoria_de(o)
            if p["chollazos"] and d >= UMBRAL_CHOLLAZOS:
                telegram(formatear(o, d, a, cat, chollazo=True))
            elif p["categorias"].get(cat, True) and d >= p["umbral"]:
                telegram(formatear(o, d, a, cat))
        if p["apps_ios"]:
            for o in apps:
                if o["id"] not in vistos:
                    telegram(formatear_app(o, precio_anterior(o)))

    for o in ofertas:
        if o["id"] not in vistos:
            estado["vistos"].append(o["id"])
            vistos.add(o["id"])


# --------------------------- GitHub (estado y relanzar) -------------------
EN_GITHUB = bool(os.environ.get("GITHUB_ACTIONS"))


def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True)


def git_guardar():
    """Sube vistos.json al repositorio para no perder el estado."""
    if not EN_GITHUB:
        return
    git("config", "user.name", "bot-chollos")
    git("config", "user.email", "bot-chollos@users.noreply.github.com")
    git("add", "vistos.json")
    if git("diff", "--staged", "--quiet").returncode == 0:
        return
    git("commit", "-q", "-m", "Actualiza estado")
    git("pull", "--rebase", "-q", "origin", "main")
    r = git("push", "-q", "origin", "HEAD:main")
    if r.returncode:
        print("No se pudo subir el estado:", r.stderr[-300:])


def huella_codigo(ref):
    return git("rev-parse", f"{ref}:bot.py").stdout.strip() + \
        git("rev-parse", f"{ref}:.github/workflows/alertas.yml").stdout.strip()


def codigo_cambiado(huella_inicial):
    if not EN_GITHUB:
        return False
    git("fetch", "-q", "origin", "main")
    return huella_codigo("origin/main") not in ("", huella_inicial)


def relanzar():
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        return
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/actions/workflows/alertas.yml/dispatches",
        data=json.dumps({"ref": "main", "inputs": {"modo": "normal"}}).encode(),
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "User-Agent": "chollos-bot"}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=20)
        print("Siguiente tanda programada.")
    except Exception as e:
        print("No se pudo relanzar:", e)


# --------------------------------- Main ----------------------------------
def main():
    estado = cargar_estado()
    estado.setdefault("vistos", [])

    if MODO == "diagnostico":
        diagnostico(estado)   # no toca Telegram ni el estado del bot en marcha
        return

    huella = huella_codigo("HEAD") if EN_GITHUB else ""
    presentar_version(estado)
    guardar_estado(estado)
    git_guardar()

    inicio = time.time()
    ultimo_feed = ultimo_guardado = ultima_comprobacion = 0.0
    while time.time() - inicio < DURACION:
        ahora = time.time()
        if ahora - ultimo_feed >= INTERVALO:
            try:
                revisar_ofertas(estado)
            except Exception as ex:
                print("Error revisando ofertas:", ex)
            guardar_estado(estado)
            ultimo_feed = time.time()
        if ahora - ultimo_guardado >= 900:
            git_guardar()
            ultimo_guardado = time.time()
        if ahora - ultima_comprobacion >= 600:
            ultima_comprobacion = time.time()
            if codigo_cambiado(huella):
                print("Hay una versión nueva del bot: reinicio para cargarla.")
                break
        espera = int(max(1, min(50, INTERVALO - (time.time() - ultimo_feed))))
        try:
            cambios = procesar_telegram(estado, espera)
        except Exception as ex:
            print("Error atendiendo Telegram:", ex)
            cambios = False
            time.sleep(5)
        if cambios:
            guardar_estado(estado)
            git_guardar()
            ultimo_guardado = time.time()

    guardar_estado(estado)
    git_guardar()
    relanzar()


if __name__ == "__main__":
    main()
    # en modo bucle un fallo puntual de Telegram no debe marcar la tanda como fallida
    sys.exit(1 if fallo_telegram and MODO == "diagnostico" else 0)
