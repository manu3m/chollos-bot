#!/usr/bin/env python3
"""
Bot de alertas de chollos de Amazon con descuentos enormes -> Telegram.

Lee las ofertas nuevas que se publican en Chollometro, se queda con las de
Amazon cuyo descuento sea igual o mayor que DESCUENTO_MIN y te las manda a
Telegram. Pensado para ejecutarse gratis cada 15 minutos con GitHub Actions.
"""
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

# ----------------------------- Configuración -----------------------------
TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
DESCUENTO_MIN = float(os.environ.get("DESCUENTO_MIN") or 80)
MODO = (os.environ.get("MODO") or "normal").strip().lower()
TIENDA = (os.environ.get("TIENDA") or "amazon").strip().lower()
FEEDS = [f.strip() for f in (os.environ.get("FEEDS") or
         "https://www.chollometro.com/rss/nuevos,https://www.chollometro.com/rss"
         ).split(",") if f.strip()]
_excluir = (os.environ.get("EXCLUIR") or "kindle,ebook,e-book").strip()
EXCLUIR = [] if _excluir.lower() == "ninguno" else [
    p.strip().lower() for p in _excluir.split(",") if p.strip()]

ESTADO = Path(__file__).with_name("vistos.json")
MAX_VISTOS = 3000      # ids recordados para no repetir avisos
MAX_PAGINAS = 15       # páginas de oferta consultadas por ejecución
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")

PALABRAS_ANTES = (r"(?:antes|pvpr?|p\.v\.p\.?r?\.?|precio\s(?:anterior|habitual|"
                  r"original|recomendado|normal)|era|en\svez\sde|en\slugar\sde|"
                  r"sin\sdescuento)")

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
             "tienda": "", "precio": None}
        for hijo in item:
            local = hijo.tag.split("}")[-1].lower()
            valor = (hijo.text or "").strip()
            if local == "title":
                o["titulo"] = limpiar(valor)
            elif local == "link":
                o["link"] = valor or hijo.get("href", "")
            elif local in ("guid", "id"):
                o["id"] = valor
            elif local == "merchant":
                o["tienda"] = hijo.get("name", "") or valor
                o["precio"] = precio_de(hijo.get("price", ""))
            elif local in ("description", "summary", "content", "encoded"):
                o["texto"] += " " + limpiar(valor)
        o["id"] = o["id"] or o["link"]
        if o["id"]:
            ofertas.append(o)
    return ofertas


# -------------------------- Cálculo del descuento ------------------------
def calcular_descuento(o):
    texto = f"{o['titulo']} {o['texto']}"
    candidatos = []

    # 1) Porcentaje escrito: "-85%", "85% de descuento", "90 % dto"
    patron = (r"-\s?(\d{1,2}(?:[.,]\d)?)\s?%|(\d{1,2}(?:[.,]\d)?)\s?%\s?"
              r"(?:de\s)?(?:dto|descuento|rebaja|off)")
    for m in re.finditer(patron, texto, re.I):
        v = a_numero(m.group(1) or m.group(2))
        if v and 0 < v < 100:
            candidatos.append(v)

    # 2) Precio actual frente a precio anterior ("antes 99€", "PVPR 120€")
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


# ------------------------------- Telegram --------------------------------
def telegram(texto):
    global fallo_telegram
    if not TOKEN or not CHAT_ID:
        print("[Telegram no configurado]\n" + texto + "\n")
        return
    datos = urllib.parse.urlencode({
        "chat_id": CHAT_ID, "text": texto, "parse_mode": "HTML",
        "disable_web_page_preview": "false"}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage", data=datos)
    try:
        urllib.request.urlopen(req, timeout=20).read()
    except urllib.error.HTTPError as e:
        fallo_telegram = True
        print("Error de Telegram:", e.code, e.read().decode(errors="replace"))
    except Exception as e:
        fallo_telegram = True
        print("Error de Telegram:", e)
    time.sleep(1.1)  # Telegram limita a ~1 mensaje por segundo


def euros(v):
    return "GRATIS" if v == 0 else f"{v:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


def formatear(o, desc, actual):
    e = html.escape
    lineas = [f"🔥 <b>-{desc:.0f}%</b> en Amazon", f"<b>{e(o['titulo'])}</b>"]
    if actual is not None:
        lineas.append(f"💶 {euros(actual)}")
    lineas.append(f'👉 <a href="{e(o["link"], quote=True)}">Ver oferta</a>')
    return "\n".join(lineas)


# -------------------------------- Estado ---------------------------------
def cargar_estado():
    try:
        return json.loads(ESTADO.read_text(encoding="utf-8"))
    except Exception:
        return {"vistos": [], "fallando": False, "iniciado": False}


def guardar_estado(estado):
    estado["vistos"] = estado["vistos"][-MAX_VISTOS:]
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


def main():
    estado = cargar_estado()
    vistos = set(estado.get("vistos", []))

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
        msg = ["🧪 <b>Diagnóstico del bot</b>",
               f"Ofertas leídas: {len(ofertas)}",
               f"De {TIENDA.capitalize()}: {len(de_tienda)}",
               f"Con descuento detectado: {len(con_desc)}",
               f"Umbral de aviso: {DESCUENTO_MIN:.0f}%"]
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
                 f"aparezca un descuento del {DESCUENTO_MIN:.0f}% o más.")
        estado["iniciado"] = True
    else:
        nuevas = [o for o in de_tienda if o["id"] not in vistos]
        for o, d, a in analizar(nuevas):
            if d is not None and d >= DESCUENTO_MIN:
                telegram(formatear(o, d, a))

    for o in ofertas:
        if o["id"] not in vistos:
            estado["vistos"].append(o["id"])
            vistos.add(o["id"])
    guardar_estado(estado)


if __name__ == "__main__":
    main()
    sys.exit(1 if fallo_telegram else 0)
