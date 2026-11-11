"""
Robô de alertas de passagens e milhas -> Telegram (v2).

Lê os feeds dos blogs de promoção, filtra o que interessa (Curitiba,
executiva, LATAM Pass, bônus, erro de tarifa, internacional), ignora
tutoriais e guias, e posta no canal com as datas de viagem, a validade
e as cidades de saída quando a matéria informa.

Roda sozinho no GitHub Actions. Filtros em config.json.
"""

import calendar
import html
import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path

import feedparser
import requests

BASE = Path(__file__).parent
CONFIG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
SEEN_FILE = BASE / "seen.json"
MAX_SEEN = 3000
UA = {"User-Agent": "Mozilla/5.0 (alertas-passagens)"}

TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
DRY_RUN = os.environ.get("DRY_RUN") == "1"

MESES = ["janeiro", "fevereiro", "marco", "abril", "maio", "junho", "julho",
         "agosto", "setembro", "outubro", "novembro", "dezembro"]
MESES_BONITO = {m: m.capitalize().replace("Marco", "Março") for m in MESES}


# ---------------------------------------------------------------- texto

def normalizar(texto: str) -> str:
    """Minúsculas e sem acentos, para comparar palavras com segurança."""
    texto = unicodedata.normalize("NFKD", texto or "")
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return texto.lower()


def tem(texto: str, palavras) -> bool:
    return any(re.search(r"(?<!\w)" + re.escape(normalizar(p)) + r"(?!\w)", texto) for p in palavras)


def limpar_html(bruto: str) -> str:
    bruto = re.sub(r"(?is)<(script|style|figure|figcaption|aside|nav|footer)[^>]*>.*?</\1>", " ", bruto or "")
    bruto = re.sub(r"(?s)<[^>]+>", " ", bruto)
    return re.sub(r"\s+", " ", html.unescape(bruto)).strip()


# ---------------------------------------------------------------- filtro

def maior_bonus(texto: str) -> int:
    """Maior % de bônus citado no texto (ex.: 'até 100% de bônus' -> 100)."""
    achados = re.findall(r"(\d{2,3})\s*%\s*(?:de\s+)?(?:bonus|bonificad)", texto)
    achados += re.findall(r"bonus\s+de\s+(?:ate\s+)?(\d{2,3})\s*%", texto)
    return max((int(x) for x in achados), default=0)


def e_tutorial(t: str) -> bool:
    f = CONFIG["filtros"]
    if any(t.startswith(normalizar(p)) for p in f["ignorar_se_titulo_comecar_com"]):
        return True
    return tem(t, f["ignorar_sempre_se_titulo_tiver"])


def classificar(titulo: str, resumo: str):
    """Devolve (etiqueta, prioridade) se a notícia interessa, ou None. Menor = mais importante."""
    t = normalizar(titulo)
    tudo = t + " " + normalizar(resumo)
    f = CONFIG["filtros"]

    if e_tutorial(t):
        return None
    if tem(t, f["ignorar_se_titulo_tiver"]) and not tem(t, f["palavras_voo"]):
        return None

    e_voo = tem(tudo, f["palavras_voo"])
    e_executiva = tem(t, f["palavras_executiva"]) or (tem(tudo, f["palavras_executiva"]) and tem(t, f["palavras_voo"]))
    e_cwb = tem(tudo, f["palavras_origem"])
    e_latam = tem(tudo, f["programas_principais"])
    e_erro = tem(tudo, f["palavras_erro_tarifa"])
    e_internacional = tem(t, f["destinos_internacionais"])
    bonus = maior_bonus(tudo)

    if e_erro and e_voo:
        return ("🚨 ERRO DE TARIFA", 0)
    if e_executiva and e_voo:
        return ("💺 EXECUTIVA", 1)
    if e_latam and bonus >= f["bonus_minimo_latam"]:
        return (f"💰 BÔNUS LATAM PASS ({bonus}%)", 2)
    if bonus >= f["bonus_minimo_outros_programas"] and tem(tudo, ["transferencia", "transfira", "transferir"]):
        return (f"💰 BÔNUS DE {bonus}%", 3)
    if e_latam and e_voo and tem(tudo, f["palavras_resgate_promocional"]):
        return ("🎯 PROMO LATAM PASS", 3)
    if e_cwb and e_voo:
        return ("📍 SAINDO DE CURITIBA", 4)
    if e_internacional and e_voo and tem(t, f["palavras_preco"]):
        return ("🌎 INTERNACIONAL", 5)
    return None


# ---------------------------------------------------------------- detalhes

RE_VALIDADE = re.compile(
    r"(?:valid[ao]s?|vigente|vigencia|vai|segue|termina|encerra|disponivel|disponiveis|campanha|promocao|bonus)"
    r"[^.!?]{0,45}?\b(?:ate|em)\s+(?:o\s+dia\s+|as\s+\d{1,2}(?:h\d{0,2}|:\d{2})?\s+(?:do\s+dia\s+|de\s+)?)?"
    r"(hoje|amanha|\d{1,2}/\d{1,2}(?:/\d{2,4})?|\d{1,2}\s+de\s+(?:" + "|".join(MESES) + r"))"
)
_MES = "(" + "|".join(MESES) + ")"
_ANO = r"(?:\s+(?:de\s+)?(20\d\d))?"
RE_FAIXA = re.compile(r"\b(?:de|entre)\s+" + _MES + _ANO + r"\s+(?:a|ate|e)\s+" + _MES + _ANO + r"\b")
RE_MES = re.compile(r"\b" + _MES + _ANO + r"\b")
RE_SAIDA = re.compile(r"\bsaindo\s+de\s+([a-z ,]+?)(?=\s+(?:para|com|a partir|por|rumo|em direcao)\b|[.!:;(]|$)")


def normalizar_com_mapa(texto: str):
    """Como normalizar(), mas guarda de qual posição do original veio cada letra."""
    texto = unicodedata.normalize("NFC", texto or "")
    saida, mapa = [], []
    for i, c in enumerate(texto):
        for d in normalizar(c):
            saida.append(d)
            mapa.append(i)
    return "".join(saida), mapa, texto


def rotulo_mes(mes: str, ano: str) -> str:
    return MESES_BONITO[mes] + (f"/{ano}" if ano else "")


def extrair_detalhes(texto_bruto: str) -> dict:
    """Procura no texto da matéria: validade, meses de viagem e cidades de saída."""
    t, mapa, original = normalizar_com_mapa(texto_bruto)
    det = {}

    s = RE_SAIDA.search(t)
    if s:
        ini, fim = mapa[s.start(1)], mapa[s.end(1) - 1] + 1
        cidades = re.sub(r"\s+", " ", original[ini:fim]).strip(" ,")
        if 3 <= len(cidades) <= 70:
            det["saida"] = cidades[0].upper() + cidades[1:]

    m = RE_VALIDADE.search(t)
    if m:
        v = m.group(1)
        det["validade"] = {"hoje": "hoje", "amanha": "amanhã"}.get(v, v.replace("marco", "março"))
        t = t[: m.start()] + " " * (m.end() - m.start()) + t[m.end():]  # o mês da validade não é mês de viagem

    meses, vistos = [], set()
    for f in RE_FAIXA.finditer(t):
        m1, a1, m2, a2 = f.groups()
        if m1 != m2:
            meses.append(f"{rotulo_mes(m1, a1)} a {rotulo_mes(m2, a2)}")
            vistos.update({m1, m2})
            t = t[: f.start()] + " " * (f.end() - f.start()) + t[f.end():]
    for mes, ano in RE_MES.findall(t):
        if mes not in vistos:
            vistos.add(mes)
            meses.append(rotulo_mes(mes, ano))
    if meses:
        det["meses"] = meses
    return det


def texto_da_pagina(url: str) -> str:
    """Baixa a matéria e devolve só o texto do artigo (sem menus nem barras laterais)."""
    try:
        r = requests.get(url, headers=UA, timeout=15)
        r.raise_for_status()
    except requests.RequestException:
        return ""
    corpo = re.search(r"(?is)<article\b.*?</article>", r.text)
    if not corpo:
        corpo = re.search(r'(?is)<div[^>]+class="[^"]*(?:entry-content|post-content|article-content)[^"]*".*?</div>', r.text)
    return limpar_html(corpo.group(0)) if corpo else ""


def detalhes_do_item(item, link: str) -> dict:
    conteudo = ""
    if item.get("content"):
        conteudo = item["content"][0].get("value", "")
    texto = limpar_html(conteudo or item.get("summary", ""))
    det = extrair_detalhes(texto)
    if CONFIG.get("buscar_detalhes_na_pagina") and ("meses" not in det or "validade" not in det):
        pagina = texto_da_pagina(link)
        if pagina:
            extra = extrair_detalhes(pagina)
            for chave, valor in extra.items():
                det.setdefault(chave, valor)
    return det


def montar_mensagem(etiqueta: str, titulo: str, link: str, fonte: str, det: dict) -> str:
    linhas = [f"<b>{etiqueta}</b>", html.escape(titulo.strip()), ""]
    if det.get("saida"):
        linhas.append(f"🛫 Saindo de: {html.escape(det['saida'])}")
    if det.get("meses"):
        meses = det["meses"]
        mostrar = ", ".join(meses[:8]) + (" e outros" if len(meses) > 8 else "")
        linhas.append(f"📅 Datas citadas: {html.escape(mostrar)}")
    if det.get("validade"):
        linhas.append(f"⏳ Válido até: {html.escape(det['validade'])}")
    if linhas[-1] != "":
        linhas.append("")
    linhas.append(f"🔗 <a href=\"{html.escape(link, quote=True)}\">Ver oferta</a> · {html.escape(fonte)}")
    return "\n".join(linhas)


# ---------------------------------------------------------------- envio e estado

def carregar_vistos() -> list:
    if SEEN_FILE.exists():
        try:
            return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
    return []


def salvar_vistos(vistos: list) -> None:
    SEEN_FILE.write_text(json.dumps(vistos[-MAX_SEEN:], ensure_ascii=False, indent=0), encoding="utf-8")


def enviar(texto: str) -> bool:
    if DRY_RUN:
        print("---- [simulação] mensagem ----\n" + texto + "\n")
        return True
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": texto, "parse_mode": "HTML", "disable_web_page_preview": False},
        timeout=20,
    )
    if r.status_code == 429:  # muitas mensagens seguidas: espera e tenta de novo
        espera = r.json().get("parameters", {}).get("retry_after", 5)
        time.sleep(espera + 1)
        return enviar(texto)
    if not r.ok:
        print(f"Erro do Telegram ({r.status_code}): {r.text}", file=sys.stderr)
    return r.ok


def main() -> int:
    if not DRY_RUN and (not TOKEN or not CHAT_ID):
        print("Faltam os segredos TELEGRAM_TOKEN e/ou TELEGRAM_CHAT_ID no GitHub.", file=sys.stderr)
        return 1

    vistos = carregar_vistos()
    vistos_set = set(vistos)
    primeira_vez = not vistos
    agora = time.time()

    candidatos = []
    for fonte in CONFIG["feeds"]:
        try:
            feed = feedparser.parse(fonte["url"], agent=UA["User-Agent"])
        except Exception as e:  # um blog fora do ar não derruba os outros
            print(f"Falha lendo {fonte['nome']}: {e}", file=sys.stderr)
            continue
        if feed.bozo and not feed.entries:
            print(f"Feed sem itens: {fonte['nome']} ({feed.get('bozo_exception')})", file=sys.stderr)
            continue
        for item in feed.entries:
            chave = item.get("link") or item.get("id") or item.get("title")
            if not chave or chave in vistos_set:
                continue
            vistos_set.add(chave)
            publicado = item.get("published_parsed") or item.get("updated_parsed")
            idade_h = (agora - calendar.timegm(publicado)) / 3600 if publicado else 0
            classe = classificar(item.get("title", ""), item.get("summary", ""))
            candidatos.append((chave, fonte["nome"], item, classe, idade_h))

    # Na primeira execução só manda o que é das últimas 24h (evita enxurrada).
    max_horas = 24 if primeira_vez else CONFIG["ignorar_noticias_mais_velhas_que_horas"]
    para_enviar = [c for c in candidatos if c[3] and c[4] <= max_horas]
    para_enviar.sort(key=lambda c: (c[3][1], c[4]))
    para_enviar = para_enviar[: CONFIG["maximo_por_rodada"]]

    enviados = 0
    falhou = set()
    for chave, nome_fonte, item, (etiqueta, _), _ in para_enviar:
        det = detalhes_do_item(item, chave)
        texto = montar_mensagem(etiqueta, item.get("title", ""), chave, nome_fonte, det)
        if enviar(texto):
            enviados += 1
            time.sleep(1.5)
        else:
            falhou.add(chave)

    # Marca como visto tudo que foi lido (menos o que falhou ao enviar, para tentar de novo).
    for chave, *_ in candidatos:
        if chave not in falhou:
            vistos.append(chave)
    salvar_vistos(vistos)

    print(f"Lidas {len(candidatos)} notícias novas, enviadas {enviados}, falhas {len(falhou)}.")
    return 1 if falhou and not enviados else 0


if __name__ == "__main__":
    sys.exit(main())
