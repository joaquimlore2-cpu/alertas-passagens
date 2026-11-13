"""
Caçador de passagens em classe executiva saindo de Curitiba -> Telegram.

Usa a busca "Explorar" do Google Flights (via SerpApi) por região, guarda o
histórico de preço de cada destino em precos.json e avisa no canal quando:
  - o preço fica abaixo do teto da região (config.json -> executiva.regioes), ou
  - o preço fica X% abaixo do preço normal daquele destino (mediana do histórico).

Roda no GitHub Actions duas vezes por dia. Ajustes em config.json -> "executiva".
Modo teste (TESTE=1): faz as buscas e mostra no log o que seria enviado, sem postar.
"""

import datetime as dt
import html
import json
import os
import statistics
import sys
import time
import urllib.parse
from pathlib import Path

import requests

BASE = Path(__file__).parent
CONFIG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))["executiva"]
PRECOS_FILE = BASE / "precos.json"

SERPAPI_KEY = os.environ.get("SERPAPI_KEY", "").strip()
TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
TESTE = os.environ.get("TESTE") == "1"

MAX_OBS_POR_DESTINO = 60


# ---------------------------------------------------------------- estado

def carregar_estado() -> dict:
    if PRECOS_FILE.exists():
        try:
            return json.loads(PRECOS_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"uso": {}, "destinos": {}}


def salvar_estado(estado: dict) -> None:
    PRECOS_FILE.write_text(json.dumps(estado, ensure_ascii=False, indent=1), encoding="utf-8")


def mes_atual() -> str:
    return dt.date.today().strftime("%Y-%m")


# ---------------------------------------------------------------- busca

def para_numero(valor):
    """flight_price pode vir como número ou texto ('R$ 7.890')."""
    if isinstance(valor, (int, float)):
        return float(valor)
    if isinstance(valor, str):
        digitos = "".join(c for c in valor if c.isdigit())
        return float(digitos) if digitos else None
    return None


def buscar_regiao(regiao: dict) -> list:
    params = {
        "engine": "google_travel_explore",
        "departure_id": CONFIG["origem"],
        "travel_class": 3,  # executiva
        "currency": "BRL",
        "gl": "br",
        "hl": "pt-br",
        "api_key": SERPAPI_KEY,
    }
    if regiao.get("area_id"):
        params["arrival_area_id"] = regiao["area_id"]
    params.update(CONFIG.get("parametros_extras", {}))

    r = requests.get("https://serpapi.com/search.json", params=params, timeout=90)
    try:
        dados = r.json()
    except ValueError:
        raise RuntimeError(f"resposta inválida da SerpApi (HTTP {r.status_code})")
    if dados.get("error"):
        raise RuntimeError(dados["error"])
    return dados.get("destinations", []) or []


# ---------------------------------------------------------------- análise

def chave_destino(d: dict) -> str:
    aero = (d.get("destination_airport") or {}).get("code")
    return aero or d.get("destination_id") or d.get("name", "?")


def link_google_flights(d: dict) -> str:
    if d.get("link"):
        return d["link"]
    destino = (d.get("destination_airport") or {}).get("code") or d.get("name", "")
    q = f"Flights to {destino} from {CONFIG['origem']} business class"
    if d.get("start_date"):
        q += f" on {d['start_date']}"
        if d.get("end_date"):
            q += f" through {d['end_date']}"
    return "https://www.google.com/travel/flights?hl=pt-BR&curr=BRL&q=" + urllib.parse.quote(q)


def formatar_reais(valor: float) -> str:
    return "R$ " + f"{valor:,.0f}".replace(",", ".")


def formatar_data(iso: str) -> str:
    try:
        return dt.date.fromisoformat(iso).strftime("%d/%m/%Y")
    except (TypeError, ValueError):
        return iso or ""


def avaliar(d: dict, preco: float, regiao: dict, historico: list):
    """Decide se vale alertar. Devolve (motivo, normal) ou None."""
    regras = CONFIG["regras"]
    precos_antigos = [o["p"] for o in historico]
    normal = statistics.median(precos_antigos) if len(precos_antigos) >= regras["minimo_de_observacoes"] else None

    abaixo_do_teto = regiao.get("teto") and preco <= regiao["teto"]
    queda = (1 - preco / normal) * 100 if normal else 0
    abaixo_do_normal = normal and queda >= regras["percentual_abaixo_do_normal"]

    if abaixo_do_normal:
        return (f"📉 {queda:.0f}% abaixo do normal (normalmente ~{formatar_reais(normal)})", normal)
    if abaixo_do_teto:
        return (f"🎯 Abaixo do seu teto de {formatar_reais(regiao['teto'])} para {regiao['nome']}", normal)
    return None


def ja_avisado_recente(info: dict, preco: float) -> bool:
    """Não repete o mesmo destino, a não ser que caia mais ou que já tenha passado um tempo."""
    ultimo = info.get("ultimo_alerta")
    if not ultimo:
        return False
    regras = CONFIG["regras"]
    dias = (dt.date.today() - dt.date.fromisoformat(ultimo["data"])).days
    caiu_mais = preco <= ultimo["p"] * (1 - regras["nova_queda_para_reavisar_percentual"] / 100)
    return dias < regras["dias_sem_repetir_destino"] and not caiu_mais


def montar_mensagem(d: dict, preco: float, motivo: str, regiao: dict) -> str:
    nome = d.get("name", "?")
    pais = d.get("country")
    aero = (d.get("destination_airport") or {}).get("code")
    titulo = f"Curitiba → {nome}" + (f", {pais}" if pais and pais != nome else "") + (f" ({aero})" if aero else "")

    linhas = ["<b>💺 EXECUTIVA BARATA</b>", html.escape(titulo), ""]
    linhas.append(f"💰 <b>{formatar_reais(preco)}</b> ida e volta, por pessoa")
    linhas.append(html.escape(motivo))
    if d.get("start_date"):
        datas = formatar_data(d["start_date"]) + (f" a {formatar_data(d['end_date'])}" if d.get("end_date") else "")
        linhas.append(f"📅 Exemplo de datas: {datas}")
    detalhes = []
    if d.get("airline"):
        detalhes.append(d["airline"])
    escalas = d.get("number_of_stops")
    if escalas is not None:
        detalhes.append("voo direto" if escalas == 0 else f"{escalas} escala" + ("s" if escalas > 1 else ""))
    if d.get("flight_duration"):
        h, m = divmod(int(d["flight_duration"]), 60)
        detalhes.append(f"{h}h{m:02d} de viagem")
    if detalhes:
        linhas.append("✈️ " + html.escape(" · ".join(detalhes)))
    linhas += ["", "⚠️ Preço do Google Flights no momento da busca; confira antes de comprar.",
               f"🔗 <a href=\"{html.escape(link_google_flights(d), quote=True)}\">Ver no Google Flights</a>"]
    return "\n".join(linhas)


# ---------------------------------------------------------------- envio

def enviar(texto: str) -> bool:
    if TESTE:
        print("---- [teste] mensagem que seria enviada ----\n" + texto + "\n")
        return True
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": texto, "parse_mode": "HTML", "disable_web_page_preview": True},
        timeout=20,
    )
    if r.status_code == 429:
        time.sleep(r.json().get("parameters", {}).get("retry_after", 5) + 1)
        return enviar(texto)
    if not r.ok:
        print(f"Erro do Telegram ({r.status_code}): {r.text}", file=sys.stderr)
    return r.ok


# ---------------------------------------------------------------- principal

def main() -> int:
    if not CONFIG.get("ativo", True):
        print("Caçador de executiva desligado em config.json (executiva.ativo = false).")
        return 0
    if not SERPAPI_KEY:
        print("Falta o segredo SERPAPI_KEY no GitHub (Settings → Secrets and variables → Actions).", file=sys.stderr)
        return 1
    if not TESTE and (not TOKEN or not CHAT_ID):
        print("Faltam os segredos TELEGRAM_TOKEN e/ou TELEGRAM_CHAT_ID no GitHub.", file=sys.stderr)
        return 1

    estado = carregar_estado()
    uso_mes = estado["uso"].get(mes_atual(), 0)
    limite = CONFIG["limite_de_buscas_por_mes"]
    hoje = dt.date.today().isoformat()

    alertas, falhas = [], 0
    for regiao in CONFIG["regioes"]:
        if uso_mes >= limite:
            print(f"Limite de {limite} buscas do mês atingido; parando para não estourar o plano grátis.")
            break
        try:
            destinos = buscar_regiao(regiao)
        except (requests.RequestException, RuntimeError) as e:
            print(f"Falha buscando {regiao['nome']}: {e}", file=sys.stderr)
            falhas += 1
            uso_mes += 1
            continue
        uso_mes += 1
        print(f"\n== {regiao['nome']}: {len(destinos)} destinos")

        for d in destinos:
            preco = para_numero(d.get("flight_price"))
            if not preco:
                continue
            chave = chave_destino(d)
            info = estado["destinos"].setdefault(chave, {"nome": d.get("name"), "obs": []})
            resultado = avaliar(d, preco, regiao, info["obs"])
            print(f"   {d.get('name')} ({chave}): {formatar_reais(preco)}"
                  + (f"  ← ALERTA: {resultado[0]}" if resultado else ""))

            # registra a observação (no máximo uma por dia por destino: fica a menor)
            if info["obs"] and info["obs"][-1]["d"] == hoje:
                info["obs"][-1]["p"] = min(info["obs"][-1]["p"], preco)
            else:
                info["obs"].append({"d": hoje, "p": preco})
            info["obs"] = info["obs"][-MAX_OBS_POR_DESTINO:]

            if resultado and not ja_avisado_recente(info, preco):
                alertas.append((preco, d, resultado[0], regiao, chave))

    estado["uso"][mes_atual()] = uso_mes

    # um alerta por aeroporto (ex.: Miami e Fort Lauderdale saem os dois por MIA)
    unicos = {}
    for a in sorted(alertas, key=lambda a: a[0]):
        unicos.setdefault(a[4], a)
    alertas = list(unicos.values())
    enviados = 0
    for preco, d, motivo, regiao, chave in alertas[: CONFIG["maximo_de_alertas_por_rodada"]]:
        if enviar(montar_mensagem(d, preco, motivo, regiao)):
            enviados += 1
            if not TESTE:
                estado["destinos"][chave]["ultimo_alerta"] = {"data": hoje, "p": preco}
            time.sleep(1.5)

    salvar_estado(estado)  # no teste também guarda os preços: ajuda a formar o "preço normal"

    print(f"\nBuscas usadas no mês: {uso_mes}/{limite}. Alertas: {len(alertas)}, enviados: {enviados}.")
    return 1 if falhas == len(CONFIG["regioes"]) else 0


if __name__ == "__main__":
    sys.exit(main())
