#!/usr/bin/env python3
"""
Bot de alertas de chollos de Amazon -> Telegram, con filtro por categorías.

Lee las ofertas nuevas de Chollometro, se queda con las de Amazon y avisa:
  - de las que superen tu umbral (80 % por defecto) en las categorías que elijas;
  - de los "chollazos" (95 % o más) de cualquier categoría, si los tienes activados.
Las categorías y el umbral se cambian desde Telegram con /categorias y /umbral.
Pensado para ejecutarse gratis cada 5 minutos con GitHub Actions.
"""
import html
import json
import os
import re
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
VERSION = 2

ESTADO = Path(__file__).with_name("vistos.json")
MAX_VISTOS = 3000
MAX_PAGINAS = 15
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")

PALABRAS_ANTES = (r"(?:antes|pvpr?|p\.v\.p\.?r?\.?|precio\s(?:anterior|habitual|"
                  r"original|recomendado|normal)|era|en\svez\sde|en\slugar\sde|"
                  r"sin\sdescuento)")

AYUDA = ("🤖 <b>Comandos</b>\n"
         "/categorias – elegir departamentos y chollazos\n"
         "/umbral 85 – cambiar el descuento mínimo de aviso\n"
         "/estado – ver tu configuración actual\n\n"
         "Reviso Telegram cada 5 minutos, así que te respondo en unos minutos.")

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


def descuento_desde_pagina(o):
    """Plan B: abre la página de la oferta y busca precio y precio anterior."""
    try:
        pag = html.unescape(descargar(o["link"]))
    except Exception:
        return None, None
    m_act = re.search(r'"price"\s*:\s*"?(\d+(?:\.\d+)?)', pag)
    m_ant = re.search(r'"nextBestPrice"\s*:\s*"?(\d+(?:\.\d+)?)', pag)
    actual = float(m_act.group(1)) if m_act else None
    antes = float(m_ant.group(1)) if m_ant else None
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
        with urllib.request.urlopen(req, timeout=20) as r:
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
    lineas.append(f'👉 <a href="{e(o["link"], quote=True)}">Ver oferta</a>')
    return "\n".join(lineas)


# ------------------------- Preferencias y menú ---------------------------
def preferencias(estado):
    p = estado.setdefault("prefs", {})
    cats = p.setdefault("categorias", {})
    for _, feed in CATEGORIAS:
        cats.setdefault(feed, True)
    p.setdefault("chollazos", True)
    p.setdefault("umbral", DESCUENTO_MIN)
    return p


def texto_menu(p):
    return ("⚙️ <b>Filtros de ofertas</b>\n"
            "Toca un departamento para activarlo ✅ o desactivarlo ⬜.\n\n"
            f"🔥 <b>Avisos normales:</b> descuento del {p['umbral']:.0f}% o más, "
            "solo en los departamentos marcados.\n"
            f"💥 <b>Chollazos:</b> descuento del {UMBRAL_CHOLLAZOS:.0f}% o más, "
            "de cualquier departamento (aunque esté desmarcado).\n\n"
            "<i>Los cambios se aplican en unos minutos.</i>")


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
            f"Departamentos: {', '.join(activas) if activas else 'ninguno'}")


def procesar_telegram(estado):
    """Lee lo que le has escrito o pulsado al bot desde la última ejecución."""
    p = preferencias(estado)
    if not TOKEN or not CHAT_ID:
        return
    offset = estado.get("tg_offset", 0)
    r = tg("getUpdates", offset=offset, timeout=0,
           allowed_updates=["message", "callback_query"])
    if not r or not r.get("ok"):
        return
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
            elif d in ("all", "none"):
                for _, feed in CATEGORIAS:
                    p["categorias"][feed] = d == "all"
            menus.add(msg.get("message_id"))
            tg("answerCallbackQuery", callback_query_id=cq["id"])
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
            elif texto:
                telegram(AYUDA)
    for mid in menus:
        if mid:
            tg("editMessageText", chat_id=CHAT_ID, message_id=mid, text=texto_menu(p),
               parse_mode="HTML", reply_markup=teclado(p))
    estado["tg_offset"] = offset


def presentar_version(estado):
    if estado.get("version", 1) >= VERSION or not TOKEN or not CHAT_ID:
        return
    tg("setMyCommands", commands=[
        {"command": "categorias", "description": "Elegir departamentos y chollazos"},
        {"command": "umbral", "description": "Cambiar el descuento mínimo (ej. /umbral 85)"},
        {"command": "estado", "description": "Ver tu configuración"},
    ])
    telegram("🆕 <b>Novedad: filtro por departamentos</b>\n"
             "Ahora puedes elegir de qué departamentos quieres avisos, y recibir "
             "siempre los chollazos de más del "
             f"{UMBRAL_CHOLLAZOS:.0f}%. Escribe /categorias cuando quieras cambiarlo.")
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


def main():
    estado = cargar_estado()
    estado.setdefault("vistos", [])
    p = preferencias(estado)
    procesar_telegram(estado)
    presentar_version(estado)
    vistos = set(estado["vistos"])

    ofertas, errores = {}, []
    for url in FEEDS:
        try:
            for o in leer_feed(url):
                ofertas.setdefault(o["id"], o)
        except Exception as ex:
            errores.append(f"{url} → {ex}")
            print("Error leyendo", url, "→", ex)
    ofertas = list(ofertas.values())

    if not ofertas:
        if not estado.get("fallando") or MODO == "diagnostico":
            telegram("⚠️ <b>No he podido leer las ofertas.</b>\n"
                     + html.escape("\n".join(errores))[:900])
        estado["fallando"] = True
        guardar_estado(estado)
        return
    if estado.get("fallando"):
        telegram("✅ Vuelvo a leer ofertas con normalidad.")
        estado["fallando"] = False

    de_tienda = [o for o in ofertas if es_de_tienda(o) and not excluido(o)]

    if MODO == "diagnostico":
        res = analizar(de_tienda)
        con_desc = sorted([r for r in res if r[1] is not None],
                          key=lambda r: r[1], reverse=True)
        por_cat = {}
        for o in de_tienda:
            c = nombre_corto(categoria_de(o))
            por_cat[c] = por_cat.get(c, 0) + 1
        msg = ["🧪 <b>Diagnóstico del bot</b>",
               f"Ofertas leídas: {len(ofertas)}",
               f"De {TIENDA.capitalize()}: {len(de_tienda)}",
               f"Con descuento detectado: {len(con_desc)}",
               f"Umbral de aviso: {p['umbral']:.0f}% · Chollazos: {UMBRAL_CHOLLAZOS:.0f}%",
               "Por departamento: " + html.escape(", ".join(
                   f"{c} {n}" for c, n in sorted(por_cat.items(), key=lambda x: -x[1])))]
        if errores:
            msg.append("Fuentes con error: " + html.escape("; ".join(errores))[:400])
        if con_desc:
            msg.append("\n<b>Mayores descuentos ahora mismo:</b>")
            for o, d, a in con_desc[:5]:
                msg.append(f'• -{d:.0f}% <a href="{html.escape(o["link"], quote=True)}">'
                           f'{html.escape(o["titulo"][:70])}</a>')
        telegram("\n".join(msg))
        guardar_estado(estado)
        return

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

    for o in ofertas:
        if o["id"] not in vistos:
            estado["vistos"].append(o["id"])
            vistos.add(o["id"])
    guardar_estado(estado)


if __name__ == "__main__":
    main()
    sys.exit(1 if fallo_telegram else 0)
