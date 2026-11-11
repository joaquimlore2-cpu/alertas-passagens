"""
Robô de alertas de passagens e milhas -> Telegram.

Lê os feeds dos principais blogs de promoção, filtra o que interessa
(saída de Curitiba, executiva, LATAM Pass, bônus de transferência,
erro de tarifa, internacional) e posta no canal do Telegram.

Roda sozinho no GitHub Actions (ver .github/workflows/alertas.yml).
As regras de filtro ficam em config.json — dá pra editar pelo site do GitHub.
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

TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
DRY_RUN = os.environ.get("DRY_RUN") == "1"


def normalizar(texto: str) -> str:
    """Minúsculas e sem acentos, para comparar palavras com segurança."""
    texto = unicodedata.normalize("NFKD", texto or "")
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return texto.lower()


def tem(texto: str, palavras) -> bool:
    return any(re.search(r"(?<!\w)" + re.escape(normalizar(p)) + r"(?!\w)", texto) for p in palavras)


def maior_bonus(texto: str) -> int:
    """Maior % de bônus citado no texto (ex.: 'até 100% de bônus' -> 100)."""
    achados = re.findall(r"(\d{2,3})\s*%\s*(?:de\s+)?(?:bonus|bonificad)", texto)
    achados += re.findall(r"bonus\s+de\s+(?:ate\s+)?(\d{2,3})\s*%", texto)
    return max((int(x) for x in achados), default=0)


def classificar(titulo: str, resumo: str):
    """
    Devolve (etiqueta, prioridade) se a notícia interessa, ou None.
    Prioridade menor = mais importante (vai primeiro).
    """
    t = normalizar(titulo)
    tudo = t + " " + normalizar(resumo)
    f = CONFIG["filtros"]

    if tem(t, f["ignorar_se_titulo_tiver"]) and not tem(t, f["palavras_voo"]):
        return None

    e_voo = tem(tudo, f["palavras_voo"])
    e_executiva = tem(tudo, f["palavras_executiva"])
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


def mostrar_chat_ids() -> None:
    """Ajuda a descobrir o CHAT_ID: lista as conversas recentes do bot."""
    r = requests.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates", timeout=20)
    print("Conversas recentes do bot (use o id como TELEGRAM_CHAT_ID):")
    for upd in r.json().get("result", []):
        msg = upd.get("message") or upd.get("channel_post") or upd.get("my_chat_member") or {}
        chat = msg.get("chat", {})
        if chat:
            print(f"  id={chat.get('id')}  tipo={chat.get('type')}  nome={chat.get('title') or chat.get('username')}")


def main() -> int:
    if not DRY_RUN:
        if not TOKEN:
            print("Falta o segredo TELEGRAM_TOKEN no GitHub.", file=sys.stderr)
            return 1
        if not CHAT_ID:
            mostrar_chat_ids()
            print("Falta o segredo TELEGRAM_CHAT_ID no GitHub.", file=sys.stderr)
            return 1

    vistos = carregar_vistos()
    vistos_set = set(vistos)
    primeira_vez = not vistos
    agora = time.time()

    candidatos = []
    for fonte in CONFIG["feeds"]:
        try:
            feed = feedparser.parse(fonte["url"], agent="Mozilla/5.0 (alertas-passagens)")
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
        texto = (
            f"<b>{etiqueta}</b>\n"
            f"{html.escape(item.get('title', '').strip())}\n\n"
            f"🔗 <a href=\"{html.escape(chave, quote=True)}\">Ver oferta</a> · {html.escape(nome_fonte)}"
        )
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
