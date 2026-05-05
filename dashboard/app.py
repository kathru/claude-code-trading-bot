import os
import sys
import time
import json
import asyncio
import requests
from datetime import datetime
from typing import List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, FileResponse
from dotenv import load_dotenv

from exchange.coinbase import CoinbaseClient
from paper_trading.engine import PaperTradingEngine, TAKER_FEE
# ── Suite agressiva v2 (alvo +65%): trend-following + momentum ─────
from strategies.donchian_breakout import DonchianBreakout
from strategies.ema_pullback     import EMAPullback
from strategies.macd_momentum    import MACDMomentum
from strategies.stoch_bounce     import StochBounce
from strategies.volatility_guard import VolatilityGuard
from strategies.trend_filter     import TrendFilter
# Estratégias antigas preservadas para referência:
# from strategies.rsi_divergence import RSIDivergence
# from strategies.support_resistance import SupportResistance
# from strategies.bb_squeeze import BBSqueeze
# from strategies.golden_cross import GoldenCross
from logger import setup_logger, log_cycle, log_trade, log_portfolio
from notifier import notify_trade

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), "code.env"))

app = FastAPI()
HTML_FILE    = os.path.join(os.path.dirname(__file__), "templates", "index.html")
STATIC_DIR   = os.path.join(os.path.dirname(__file__), "static")

from fastapi.staticfiles import StaticFiles
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
HISTORY_FILE  = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "portfolio_history.json")


# ── Cache de cotação USD/BRL ──────────────────────────────────────
USD_BRL_TTL = 1800           # atualiza a cada 30 minutos
_usd_brl_cache: dict = {"rate": 5.70, "ts": 0.0}

def _fetch_usd_brl() -> float:
    now = time.time()
    if now - _usd_brl_cache["ts"] < USD_BRL_TTL:
        return _usd_brl_cache["rate"]
    try:
        r = requests.get("https://api.frankfurter.app/latest?from=USD&to=BRL", timeout=5)
        rate = float(r.json()["rates"]["BRL"])
        _usd_brl_cache["rate"] = rate
        _usd_brl_cache["ts"]   = now
        return rate
    except Exception:
        return _usd_brl_cache["rate"]


# ── Fear & Greed Index (alternative.me) ──────────────────────────
_fg_cache: dict = {"value": 50, "label": "Neutral", "ts": 0.0}

def _fetch_fear_greed() -> dict:
    now = time.time()
    if now - _fg_cache["ts"] < FG_TTL:
        return _fg_cache
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=6)
        d = r.json()["data"][0]
        _fg_cache["value"] = int(d["value"])
        _fg_cache["label"] = d["value_classification"]
        _fg_cache["ts"]    = now
    except Exception:
        pass   # mantém cache anterior em caso de falha
    return _fg_cache


# ── Cache de candles por par ──────────────────────────────────────
# Refresh mais frequente para acompanhar cycle de 90s e capturar breakouts
CANDLE_TTL = 240             # 4 min — suficiente para 30min/1H candles
_candle_cache: dict = {}     # {pair: {"data": [...], "ts": float}}

def _get_candles(pair: str, granularity: str, limit: int = 100) -> list:
    key = f"{pair}:{granularity}"
    now = time.time()
    cached = _candle_cache.get(key)
    if cached and (now - cached["ts"]) < CANDLE_TTL:
        return cached["data"]
    data = client.get_candles(pair, granularity=granularity, limit=limit)
    _candle_cache[key] = {"data": data, "ts": now}
    return data


# ── Preço anterior por par (para log de variação) ────────────────
_last_prices: dict = {}      # {pair: float}


def _dynamic_tp(fg_value: int) -> float:
    """TP dinâmico entre 3% e 8% inversamente proporcional ao Fear & Greed.
    Mercado com medo   → alvo maior (deixa winners correrem mais)
    Mercado ganancioso → alvo menor (realiza antes da euforia acabar)
    """
    if   fg_value <= 25: return TAKE_PROFIT_MAX          # Medo extremo: +8%
    elif fg_value <= 40: return round(TAKE_PROFIT_MIN + (TAKE_PROFIT_MAX - TAKE_PROFIT_MIN) * 0.67, 1)  # +6.5%
    elif fg_value <= 60: return round((TAKE_PROFIT_MIN + TAKE_PROFIT_MAX) / 2, 1)  # Neutro: +5.5%
    elif fg_value <= 74: return round(TAKE_PROFIT_MIN + (TAKE_PROFIT_MAX - TAKE_PROFIT_MIN) * 0.17, 1)  # +3.8%
    else:                return TAKE_PROFIT_MIN           # Ganância extrema: +3%


def _load_history() -> list:
    try:
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return []


def _save_history(history: list):
    try:
        os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
        with open(HISTORY_FILE, "w") as f:
            json.dump(history, f)
    except Exception:
        pass


def _current_cycle() -> int:
    """Número do ciclo baseado no horário de São Paulo (UTC-3, fixo).
    Vai de #0 (meia-noite SP) a #959 (23:58:30 SP), idêntico em todos os servidores."""
    SP_OFFSET = -3 * 3600   # UTC-3 fixo — SP aboliu horário de verão em 2019
    return (int(time.time()) + SP_OFFSET) % 86400 // CYCLE_INTERVAL

PAIRS = ["BTC-USD", "ETH-USD", "SOL-USD"]

# ── Ciclo e candles ─────────────────────────────────────────────
CYCLE_INTERVAL    = 90       # ciclo de 90s — 3.3× mais reativo (era 300s)
CANDLE_30M        = "THIRTY_MINUTE"  # Donchian, Stoch
CANDLE_1H         = "ONE_HOUR"       # EMA Pullback, MACD
CANDLE_6H         = "SIX_HOUR"
CANDLE_1D         = "ONE_DAY"        # Trend, VolGuard

# ── Modelo de consenso ──────────────────────────────────────────
TRADE_PCT          = 0.01    # 1% do saldo disponível por trade (dinâmico)
CONSENSUS_BUY_MIN  = 2       # nº mínimo de estratégias para BUY
CONSENSUS_SELL_MIN = 2       # nº mínimo de estratégias para SELL fechar posição
# SL/TP/Trailing fecham posição independente do SELL score

# ── Gestão de risco ──────────────────────────────────────────────
INITIAL_SL_PCT        = 5.0  # SL: -5%
TAKE_PROFIT_MIN       = 3.0  # TP mínimo: +3% (mercado em ganância)
TAKE_PROFIT_MAX       = 8.0  # TP máximo: +8% (mercado em medo extremo)
TRAILING_STOP_PCT     = 8.0  # trailing: -8% do pico
TRAILING_ACTIVATE_PCT = 6.0  # trailing só ativa após +6%
SL_COOLDOWN_CYCLES    = 1    # após SL, espera 1 ciclo antes de re-entrar (90s)

# ── Pyramid (scale-in em posição lucrativa) ──────────────────────
PYRAMID_MAX          = 3     # máx. 3 adições (alavancagem até 3× a entrada)
PYRAMID_MIN_GAIN_PCT = 0.5   # só adiciona se ≥ +0.5% no lucro
PYRAMID_SIZE_PCT     = 0.25  # cada pyramid = 25% do trade inicial

# ── Fear & Greed ─────────────────────────────────────────────────
FG_FEAR_MAX    = 25   # Medo Extremo: consensus_min → 1, entrada direta
FG_GREED_MIN   = 75   # Ganância Extrema: fecha posições, bloqueia novas entradas
FG_TTL         = 3600 # cache de 1 hora (índice atualiza 1×/dia)

client = CoinbaseClient(os.getenv("API_KEY"), os.getenv("SECRET_KEY"))
engine = PaperTradingEngine(initial_balance_usd=10000.0)

# ── 4 estratégias agressivas independentes ──────────────────────
all_strategies = [
    DonchianBreakout(period=20, rsi_min=55.0, vol_mult=1.2),
    EMAPullback(fast=9, mid=21, slow=50, touch_tolerance_pct=0.5),
    MACDMomentum(fast=12, slow=26, signal=9, ema_filter=50),
    StochBounce(k_period=14, d_period=3, oversold=25, overbought=80, ma_filter=200),
]

# Mapa de candles por estratégia
STRAT_CANDLES = {
    "Donchian Breakout": CANDLE_30M,
    "EMA Pullback":      CANDLE_1H,
    "MACD Momentum":     CANDLE_1H,
    "Stoch Bounce":      CANDLE_30M,
}

# Guard de risco global — só dispara em crashes reais
vol_guard    = VolatilityGuard(threshold_pct=12.0, consecutive_days=3)
trend_filter = TrendFilter(period=50)   # EMA50 1H — alinhado com EMA Pullback e MACD Momentum

# ── Cooldown anti-whipsaw após SL (por slot) ─────────────────────
sl_cooldowns: dict = {}   # {f"{strat}:{pair}": cycles_remaining}

# ── Slots independentes: 4 estratégias × 3 pares + 3 manuais ────
def _empty_slot():
    return {"qty": 0.0, "entry": 0.0, "peak": 0.0,
            "realized": 0.0, "unrealized": 0.0, "pyramids": 0}

SLOTS_FILE = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "data", "strategy_slots.json")

def _load_slots() -> dict:
    slots = {}
    for s in all_strategies:
        for p in PAIRS:
            slots[f"{s.name}:{p}"] = _empty_slot()
    for p in PAIRS:
        slots[f"manual:{p}"] = _empty_slot()
    try:
        if os.path.exists(SLOTS_FILE):
            saved = json.load(open(SLOTS_FILE))
            for k, v in saved.items():
                if k in slots:
                    slots[k].update(v)
    except Exception:
        pass
    return slots

def _save_slots(slots: dict):
    try:
        os.makedirs(os.path.dirname(SLOTS_FILE), exist_ok=True)
        with open(SLOTS_FILE, "w") as f:
            json.dump(slots, f, indent=2)
    except Exception:
        pass

strategy_slots = _load_slots()

# compat aliases para endpoints manuais
def _save_manual(s): _save_slots(s)

# ── P&L por estratégia (atribuição proporcional) ─────────────────
STRAT_PNL_FILE = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "data", "strategy_pnl.json")

def _load_strategy_pnl() -> dict:
    pnl = {s.name: {"realized": 0.0, "trades": 0} for s in all_strategies}
    try:
        if os.path.exists(STRAT_PNL_FILE):
            saved = json.load(open(STRAT_PNL_FILE))
            for k, v in saved.items():
                if k in pnl:
                    pnl[k].update(v)
    except Exception:
        pass
    return pnl

def _save_strategy_pnl(pnl: dict):
    try:
        os.makedirs(os.path.dirname(STRAT_PNL_FILE), exist_ok=True)
        with open(STRAT_PNL_FILE, "w") as f:
            json.dump(pnl, f, indent=2)
    except Exception:
        pass

strategy_pnl = _load_strategy_pnl()

def _attr_pnl(strat_name: str, pnl_usd: float):
    """Atribui P&L realizado diretamente à estratégia."""
    if strat_name in strategy_pnl:
        strategy_pnl[strat_name]["realized"] += pnl_usd
        strategy_pnl[strat_name]["trades"]   += 1
    _save_strategy_pnl(strategy_pnl)
    state["strategy_pnl"] = strategy_pnl

last_signals: dict = {}   # {f"{pair}:{strat}": signal}

logger = setup_logger("dashboard")
connected_clients: List[WebSocket] = []


def _update_portfolio_state():
    total = engine.portfolio_value()
    pnl   = total - engine.initial_balance
    state["portfolio"] = {
        "usd":             round(engine.balance_usd, 2),
        "total":           round(total, 2),
        "pnl":             round(pnl, 2),
        "pnl_pct":         round((pnl / engine.initial_balance) * 100, 2),
        "holdings":        {k: round(v, 8) for k, v in engine.holdings.items()},
        "initial_balance": round(engine.initial_balance, 2),
        "total_fees_usd":  round(engine.total_fees_usd, 4),
    }
    return total, pnl


def _load_trades_from_engine() -> list:
    """Converte trades salvos no engine para o formato do dashboard."""
    result = []
    for t in reversed(engine.trades[-50:]):
        pair = t.get("symbol", "") + "-USD"
        result.append({
            "time":     t.get("time", "")[:19].replace("T", " ")[11:],
            "side":     t.get("side", ""),
            "pair":     pair,
            "price":    t.get("price", 0),
            "usd":      t.get("usd", 0),
            "fee":      t.get("fee", 0),
            "strategy": t.get("strategy", ""),
        })
    return result


state = {
    "prices":    {},
    "signals":   {},
    "slots":     strategy_slots,   # 12 slots independentes + 3 manuais
    "portfolio": {"usd": engine.balance_usd, "total": engine.balance_usd,
                  "pnl": 0.0, "pnl_pct": 0.0,
                  "initial_balance": engine.initial_balance,
                  "total_fees_usd": engine.total_fees_usd},
    "trades":    _load_trades_from_engine(),
    "feed":      [],
    "history":   _load_history(),
    "cycle":     _current_cycle(),
    "status":        "running",
    "last_update":   "",
    "cycle_start_ts": 0,
    "cycle_interval": CYCLE_INTERVAL,
    "usd_brl":          5.70,
    "trade_amount_brl": 0.0,
    "strategy_pnl":     strategy_pnl,
    "fear_greed":       {"value": 50, "label": "Neutral"},
}


@app.get("/", response_class=HTMLResponse)
async def index():
    return FileResponse(HTML_FILE)




@app.get("/candles/{pair}")
async def get_candles(pair: str, granularity: str = "FIVE_MINUTE", limit: int = 150):
    try:
        candles = client.get_candles(pair, granularity=granularity, limit=limit)
        result = []
        for c in sorted(candles, key=lambda x: int(x["start"])):
            result.append({
                "time":   int(c["start"]),
                "open":   float(c["open"]),
                "high":   float(c["high"]),
                "low":    float(c["low"]),
                "close":  float(c["close"]),
                "volume": float(c["volume"]),
            })
        return result
    except Exception as e:
        logger.error(f"get_candles error: {e}")
        return []


@app.post("/trade/buy")
async def manual_buy(pair: str, brl: float = 62.5):
    symbol = pair.split("-")[0]
    ticker = client.get_ticker(pair)
    price  = float(ticker.get("price", 0))
    if not price:
        return {"ok": False, "error": "Preço indisponível"}
    usd = brl / state["usd_brl"]
    qty = usd / price
    if not engine.buy(symbol, usd, price, "manual"):
        return {"ok": False, "error": "Saldo insuficiente"}
    engine.update_price(symbol, price)
    slot_key = f"manual:{pair}"
    strategy_slots[slot_key] = {"qty": qty, "entry": price, "peak": price,
                                 "realized": 0.0, "unrealized": 0.0}
    _save_manual(strategy_slots)
    _record_trade("BUY", pair, qty, price, usd, "manual")
    _update_portfolio_state()
    await broadcast(state)
    return {"ok": True, "qty": qty, "price": price, "usd": usd}


@app.post("/trade/sell")
async def manual_sell(pair: str, qty: float = 0, brl: float = 0):
    symbol = pair.split("-")[0]
    held   = engine.holdings.get(symbol, 0)
    if held <= 0:
        return {"ok": False, "error": f"Sem {symbol} para vender"}
    ticker = client.get_ticker(pair)
    price  = float(ticker.get("price", 0))
    if not price:
        return {"ok": False, "error": "Preço indisponível"}
    if brl > 0:
        # Converte valor em BRL para qty de cripto
        usd_value = brl / state["usd_brl"]
        sell_qty = min(usd_value / price, held)
    else:
        sell_qty = min(qty, held) if qty > 0 else held   # 0 = vender tudo
    usd = sell_qty * price * (1 - 0.006)
    if not engine.sell(symbol, sell_qty, price, "manual"):
        return {"ok": False, "error": "Falha na venda"}
    # Venda total: zera slot manual
    if sell_qty >= held:
        slot_k = f"manual:{pair}"
        if slot_k in strategy_slots:
            strategy_slots[slot_k].update({"qty": 0.0, "entry": 0.0, "peak": 0.0})
    _save_slots(strategy_slots)
    _record_trade("SELL", pair, sell_qty, price, usd, "manual")
    _update_portfolio_state()
    await broadcast(state)
    return {"ok": True, "qty": sell_qty, "price": price, "usd": usd}


@app.post("/admin/reset-portfolio")
async def reset_portfolio(token: str = ""):
    """Reset completo: R$4.000 total — R$1.000/cripto + R$1.000 caixa."""
    expected = os.getenv("RESET_TOKEN", "reset2026")
    if token != expected:
        return {"ok": False, "error": "Token inválido"}

    # ── Busca preços e câmbio ────────────────────────────────────
    usd_brl = _fetch_usd_brl()
    prices  = {}
    for pair in PAIRS:
        try:
            t = client.get_ticker(pair)
            prices[pair] = float(t.get("price", 0))
        except Exception as e:
            logger.error(f"reset: preço de {pair} indisponível: {e}")
    if not all(prices.get(p) for p in PAIRS):
        return {"ok": False, "error": f"Preços indisponíveis: {prices}"}

    TOTAL_BRL = 4000.0
    ALLOC_BRL = 1000.0                     # R$1.000 por cripto + R$1.000 caixa
    alloc_usd = ALLOC_BRL / usd_brl        # valor exato em USD sem arredondamento
    total_usd = TOTAL_BRL / usd_brl        # portfolio total em USD

    # ── Reinicia engine DIRETAMENTE — sem compra, sem taxas ──────
    # qty = alloc_usd / price  →  qty * price = alloc_usd exatamente
    # portfolio_value() = balance_usd + Σ(qty * price) = 4 * alloc_usd = total_usd
    # P&L = portfolio_value() - initial_balance = total_usd - total_usd = 0,00 ✅
    engine.initial_balance = total_usd
    engine.balance_usd     = alloc_usd     # caixa: R$1.000
    engine.holdings        = {}
    engine.entry_prices    = {}
    engine.trades          = []
    engine.total_fees_usd  = 0.0
    engine.prices          = {}

    for pair in PAIRS:
        sym   = pair.split("-")[0]
        price = prices[pair]
        qty   = alloc_usd / price           # sem desconto de fee — P&L parte de 0
        engine.holdings[sym]     = qty
        engine.entry_prices[sym] = price
        engine.prices[sym]       = price
    engine._save_state()

    # ── Reinicia todos os slots (estratégias + manual) ───────────
    # Slots das estratégias: posição inicial 1 cripto = 1 slot por par
    # Usamos um slot "reset" para registrar a posição inicial
    for s in all_strategies:
        for pair in PAIRS:
            strategy_slots[f"{s.name}:{pair}"] = _empty_slot()
    # Slot manual zerado
    for pair in PAIRS:
        strategy_slots[f"manual:{pair}"] = _empty_slot()
        # Posição inicial do reset vai pro primeiro slot por par (Donchian)
        sym   = pair.split("-")[0]
        price = prices[pair]
        qty   = engine.holdings[sym]
        first_key = f"{all_strategies[0].name}:{pair}"
        strategy_slots[first_key] = {
            "qty": qty, "entry": price, "peak": price,
            "realized": 0.0, "unrealized": 0.0, "pyramids": 0
        }
    _save_slots(strategy_slots)
    state["slots"] = strategy_slots

    # ── Reinicia P&L por estratégia ──────────────────────────────
    for name in strategy_pnl:
        strategy_pnl[name] = {"realized": 0.0, "trades": 0}
    _save_strategy_pnl(strategy_pnl)
    state["strategy_pnl"] = strategy_pnl

    # ── Reinicia histórico e feed ─────────────────────────────────
    state["history"] = []
    state["trades"]  = []
    state["feed"]    = []
    _save_history(state["history"])

    # ── Reinicia cooldowns e sinais ───────────────────────────────
    sl_cooldowns.clear()
    last_signals.clear()

    _update_portfolio_state()
    await broadcast(state)

    summary = {
        "ok":        True,
        "total_brl": TOTAL_BRL,
        "usd_brl":   round(usd_brl, 4),
        "total_usd": round(total_usd, 2),
        "cash_usd":  round(alloc_usd, 2),
        "cash_brl":  ALLOC_BRL,
        "holdings":  {
            p.split("-")[0]: {
                "qty":       round(engine.holdings[p.split("-")[0]], 6),
                "price_usd": prices[p],
                "value_brl": round(alloc_usd * usd_brl, 2)
            } for p in PAIRS
        }
    }
    logger.info(f"✅ RESET COMPLETO — {summary}")
    return summary


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    connected_clients.append(websocket)
    await websocket.send_json(state)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        connected_clients.remove(websocket)


async def broadcast(data: dict):
    dead = []
    for ws in connected_clients:
        try:
            await ws.send_json(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        connected_clients.remove(ws)


def get_rsi_value(candles, period=14):
    try:
        import pandas as pd
        df = pd.DataFrame(candles, columns=["start","low","high","open","close","volume"])
        df = df.astype({"close": float}).sort_values("start")
        delta = df["close"].diff()
        gain = delta.clip(lower=0).rolling(period).mean()
        loss = (-delta.clip(upper=0)).rolling(period).mean()
        rs = gain / loss.replace(0, float("inf"))
        rsi = 100 - (100 / (1 + rs))
        return round(float(rsi.iloc[-1]), 1)
    except Exception:
        return 50.0


def _record_trade(side, pair, qty, price, usd, strategy):
    symbol = pair.split("-")[0]
    fee = usd * TAKER_FEE if side == "BUY" else usd / (1 - TAKER_FEE) * TAKER_FEE
    log_trade(logger, side, pair, qty, price, usd, strategy)
    notify_trade(side, pair, qty, price, usd)
    state["trades"].insert(0, {
        "time": datetime.now().strftime("%H:%M:%S"),
        "side": side, "pair": pair,
        "price": price, "usd": usd,
        "fee":  round(fee, 6),
        "strategy": strategy,
    })
    state["trades"] = state["trades"][:50]


async def trading_loop():
    logger.info("Loop independente — 4 estratégias × 25%%, ciclo %ds", CYCLE_INTERVAL)
    while True:
        state["cycle"] = _current_cycle()
        now_str = datetime.now().strftime("%H:%M:%S")
        state["last_update"]    = now_str
        state["cycle_start_ts"] = int(time.time())

        usd_brl = await asyncio.get_event_loop().run_in_executor(None, _fetch_usd_brl)
        state["usd_brl"] = round(usd_brl, 4)
        state["trade_amount_brl"] = round(engine.balance_usd * TRADE_PCT * usd_brl, 2)

        # Fear & Greed (cache 1h — non-blocking via executor)
        fg = await asyncio.get_event_loop().run_in_executor(None, _fetch_fear_greed)
        state["fear_greed"] = {"value": fg["value"], "label": fg["label"]}
        fg_value       = fg["value"]
        current_tp_pct = _dynamic_tp(fg_value)   # 3%–8% dinâmico

        portfolio_total = engine.portfolio_value()

        for pair in PAIRS:
            symbol = pair.split("-")[0]
            try:
                ticker = client.get_ticker(pair)
                price  = float(ticker.get("price", 0))
                if not price:
                    continue

                _last_prices[pair] = price
                engine.update_price(symbol, price)
                state["prices"][pair] = {
                    "price":          price,
                    "price_pct_chg":  float(ticker.get("price_percentage_change_24h", 0)),
                    "volume_24h":     float(ticker.get("volume_24h", 0)),
                }

                candles_1h = _get_candles(pair, CANDLE_1H, limit=250)
                candles_6h = _get_candles(pair, CANDLE_6H, limit=100)
                candles_1d = _get_candles(pair, CANDLE_1D, limit=250)

                # Macro: tendência EMA9·21·50 em 1H (idêntico ao EMA Pullback) + vol diária
                df_1h_trend = trend_filter.candles_to_df(candles_1h)
                try:
                    import pandas as _pd
                    _c = df_1h_trend["close"]
                    _e9  = _c.ewm(span=9,  adjust=False).mean()
                    _e21 = _c.ewm(span=21, adjust=False).mean()
                    _e50 = _c.ewm(span=50, adjust=False).mean()
                    if len(_c) >= 50:
                        if float(_e9.iloc[-1]) > float(_e21.iloc[-1]) > float(_e50.iloc[-1]):
                            trend = "BUY"    # EMAs alinhadas em alta
                        elif float(_e9.iloc[-1]) < float(_e21.iloc[-1]):
                            trend = "SELL"   # EMA9 abaixo de EMA21 — perda de tendência
                        else:
                            trend = "HOLD"
                    else:
                        trend = "HOLD"
                except Exception:
                    trend = trend_filter.analyze(df_1h_trend)  # fallback
                df_1d      = vol_guard.candles_to_df(candles_1d)
                vol_signal = vol_guard.analyze(df_1d)
                pair_signals = {"Trend": trend, "Vol Guard": vol_signal}

                # ── Volatilidade extrema: fecha posição de consenso e PULA ciclo ──
                if vol_signal == "SELL":
                    # vol_guard: reduz 1%/ciclo em cada slot aberto deste par
                    max_usd = engine.balance_usd * TRADE_PCT
                    for strat in all_strategies:
                        vkey = f"{strat.name}:{pair}"
                        vslot = strategy_slots.get(vkey, _empty_slot())
                        if vslot["qty"] > 0:
                            vqty = min(vslot["qty"], max_usd / price if price > 0 else vslot["qty"])
                            vnet = vqty * price * (1 - 0.006)
                            if engine.sell(symbol, vqty, price, f"vol_guard:{strat.name}"):
                                pnl_vg = vnet - vslot["entry"] * vqty
                                vslot["realized"] += pnl_vg
                                _attr_pnl(strat.name, pnl_vg)
                                _record_trade("SELL", pair, vqty, price, vnet, f"vol_guard:{strat.name}")
                            rem = vslot["qty"] - vqty
                            if rem < 1e-8:
                                vslot["qty"] = 0.0; vslot["entry"] = 0.0; vslot["peak"] = 0.0
                            else:
                                vslot["qty"] = rem
                    _save_slots(strategy_slots)
                    logger.info(f"[{pair}] VOLATILIDADE EXTREMA — reduzindo posições 1%/ciclo")
                    # ← Não executa estratégias neste ciclo para evitar re-abertura imediata

                else:
                  # ── PASSO 1: coleta TODOS os sinais antes de agir ─────────
                  # (score completo no feed — sem notas parciais)
                  candles_30m = _get_candles(pair, CANDLE_30M, limit=250)
                  candle_map  = {
                      "Donchian Breakout": candles_30m,
                      "EMA Pullback":      candles_1h,
                      "MACD Momentum":     candles_1h,
                      "Stoch Bounce":      candles_30m,
                  }
                  signals_this_cycle = {}
                  buy_score   = 0
                  sell_score  = 0
                  sell_strats = []

                  for strat in all_strategies:
                      raw    = candle_map.get(strat.name, candles_1h)
                      df     = strat.candles_to_df(raw)
                      signal = strat.analyze(df)
                      pair_signals[strat.name]        = signal
                      signals_this_cycle[strat.name]  = signal
                      if signal == "BUY":
                          buy_score  += 1
                      elif signal == "SELL":
                          sell_score += 1
                          sell_strats.append(strat.name)

                  pair_signals["buy_score"]  = buy_score
                  pair_signals["sell_score"] = sell_score

                  # Feed: registra mudanças de sinal com score final completo
                  for strat in all_strategies:
                      signal  = signals_this_cycle[strat.name]
                      sig_key = f"{pair}:{strat.name}"
                      if signal != last_signals.get(sig_key):
                          last_signals[sig_key] = signal
                          state["feed"].insert(0, {
                              "time":     now_str,
                              "cycle":    state["cycle"],
                              "pair":     pair,
                              "strategy": strat.name,
                              "signal":   signal,
                              "price":    price,
                              "executed": False,
                              "note":     f"↑{buy_score}/4  ↓{sell_score}/4",
                          })
                          state["feed"] = state["feed"][:100]

                  # ── PASSO 2: execução independente por estratégia ────────
                  extreme_fear  = fg_value <= FG_FEAR_MAX
                  extreme_greed = fg_value >= FG_GREED_MIN

                  for strat in all_strategies:
                      key    = f"{strat.name}:{pair}"
                      slot   = strategy_slots.setdefault(key, _empty_slot())
                      signal = signals_this_cycle.get(strat.name, "HOLD")

                      if slot["qty"] > 0:
                          slot["peak"] = max(slot["peak"], price)
                          gain_pct     = (price - slot["entry"]) / slot["entry"] * 100
                          tp_hit = price >= slot["entry"] * (1 + current_tp_pct / 100)
                          sl_hit = price <= slot["entry"] * (1 - INITIAL_SL_PCT  / 100)
                          tr_act = gain_pct >= TRAILING_ACTIVATE_PCT
                          tr_hit = tr_act and price <= slot["peak"] * (1 - TRAILING_STOP_PCT / 100)

                          def _sell_slot(qty, label, is_sl=False):
                              net = qty * price * (1 - 0.006)
                              if engine.sell(symbol, qty, price, f"{strat.name}:{label}"):
                                  pnl = net - slot["entry"] * qty
                                  slot["realized"] += pnl
                                  _attr_pnl(strat.name, pnl)
                                  _record_trade("SELL", pair, qty, price, net, f"{strat.name}:{label}")
                                  logger.info(f"[{pair}][{strat.name}] SELL {label} P&L ${pnl:+.2f}")
                                  if is_sl:
                                      sl_cooldowns[key] = SL_COOLDOWN_CYCLES
                              rem = slot["qty"] - qty
                              if rem < 1e-8:
                                  slot["qty"] = 0.0; slot["entry"] = 0.0
                                  slot["peak"] = 0.0; slot["pyramids"] = 0
                              else:
                                  slot["qty"] = rem

                          if tp_hit:
                              _sell_slot(slot["qty"], f"TP+{current_tp_pct:.0f}%")
                          elif sl_hit:
                              _sell_slot(slot["qty"], f"SL-{INITIAL_SL_PCT:.0f}%", is_sl=True)
                          elif tr_hit:
                              _sell_slot(slot["qty"], f"TRAILING-{TRAILING_STOP_PCT:.0f}%")
                          elif extreme_greed or signal == "SELL":
                              # Saída gradual: 1% do saldo por ciclo
                              max_qty = engine.balance_usd * TRADE_PCT / price
                              sell_q  = min(slot["qty"], max_qty)
                              if sell_q > 1e-8:
                                  lbl = f"GREED{fg_value}" if extreme_greed else "SELL"
                                  _sell_slot(sell_q, lbl)
                          elif signal == "BUY" and gain_pct >= PYRAMID_MIN_GAIN_PCT:
                              pdone = slot.get("pyramids", 0)
                              if pdone < PYRAMID_MAX:
                                  pyr_usd = engine.balance_usd * TRADE_PCT * PYRAMID_SIZE_PCT
                                  if engine.balance_usd >= pyr_usd * 1.006:
                                      add_qty = pyr_usd / price
                                      if engine.buy(symbol, pyr_usd, price,
                                                    f"{strat.name}:pyramid{pdone+1}"):
                                          total   = slot["qty"] + add_qty
                                          slot["entry"] = (slot["qty"]*slot["entry"]+add_qty*price)/total
                                          slot["qty"]   = total
                                          slot["peak"]  = max(slot["peak"], price)
                                          slot["pyramids"] = pdone + 1
                                          _record_trade("BUY", pair, add_qty, price, pyr_usd,
                                                        f"{strat.name}:pyramid{pdone+1}")
                                          logger.info(f"[{pair}][{strat.name}] 📈 PYRAMID #{pdone+1} "
                                                      f"(gain {gain_pct:.1f}%)")

                          slot["unrealized"] = (price - slot["entry"]) * slot["qty"] if slot["qty"] > 0 else 0.0

                      elif signal == "BUY" and not extreme_greed:
                          cooldown = sl_cooldowns.get(key, 0)
                          if cooldown > 0:
                              sl_cooldowns[key] = cooldown - 1
                          else:
                              trade_usd = engine.balance_usd * TRADE_PCT
                              if engine.balance_usd >= trade_usd * 1.006:
                                  qty = trade_usd / price
                                  if engine.buy(symbol, trade_usd, price, strat.name):
                                      slot["qty"]      = qty
                                      slot["entry"]    = price
                                      slot["peak"]     = price
                                      slot["pyramids"] = 0
                                      _record_trade("BUY", pair, qty, price, trade_usd, strat.name)
                                      logger.info(f"[{pair}][{strat.name}] ✅ BUY 1% "
                                                  f"R${trade_usd*usd_brl:.0f} @ ${price:,.2f}")
                                      state["feed"].insert(0, {
                                          "time": now_str, "cycle": state["cycle"],
                                          "pair": pair, "strategy": strat.name,
                                          "signal": "BUY", "price": price,
                                          "executed": True,
                                          "note": f"R${trade_usd*usd_brl:.0f} (1% saldo)",
                                      })
                                      state["feed"] = state["feed"][:100]
                              else:
                                  logger.info(f"[{pair}][{strat.name}] BUY negado — saldo insuficiente")

                # ── Slot manual: SL/TP/Trailing (fecha 100%) ──────────────────
                ms = strategy_slots.get(f"manual:{pair}")
                if ms and ms.get("qty", 0) > 0:
                    ms["peak"] = max(ms["peak"], price)
                    g   = (price - ms["entry"]) / ms["entry"] * 100
                    tph = price >= ms["entry"] * (1 + current_tp_pct / 100)
                    slh = price <= ms["entry"] * (1 - INITIAL_SL_PCT  / 100)
                    tra = g >= TRAILING_ACTIVATE_PCT
                    trh = tra and price <= ms["peak"] * (1 - TRAILING_STOP_PCT / 100)
                    rsn = (f"TP+{current_tp_pct:.0f}%" if tph else
                           f"SL-{INITIAL_SL_PCT:.0f}%"  if slh else
                           f"TRAILING-{TRAILING_STOP_PCT:.0f}%" if trh else None)
                    if rsn:
                        net = ms["qty"] * price * (1 - 0.006)
                        if engine.sell(symbol, ms["qty"], price, f"manual:{rsn}"):
                            ms["realized"] += net - ms["entry"] * ms["qty"]
                            _record_trade("SELL", pair, ms["qty"], price, net, f"manual:{rsn}")
                            logger.info(f"[{pair}][manual] {rsn} @ ${price:,.2f}")
                        ms["qty"] = 0.0; ms["entry"] = 0.0; ms["peak"] = 0.0
                    else:
                        ms["unrealized"] = (price - ms["entry"]) * ms["qty"]

                # ── Salva slots e atualiza signals no state ───────────────────
                _save_slots(strategy_slots)
                state["slots"] = strategy_slots

                rsi_val     = get_rsi_value(candles_1h)
                entry_price = engine.entry_prices.get(symbol)
                change_pct  = ((price - entry_price) / entry_price * 100) if entry_price else None
                state["signals"][pair] = {
                    "strategies":  pair_signals,
                    "trend":       trend,
                    "vol_guard":   vol_signal,
                    "rsi":         rsi_val,
                    "entry_price": round(entry_price, 2) if entry_price else None,
                    "change_pct":  round(change_pct,  2) if change_pct is not None else None,
                    "sl_level":    round(entry_price * (1 - INITIAL_SL_PCT  / 100), 2) if entry_price else None,
                    "tp_level":    round(entry_price * (1 + current_tp_pct / 100), 2) if entry_price else None,
                }
                log_cycle(logger, state["cycle"], pair, price, pair_signals, trend)

            except Exception as e:
                logger.error(f"[{pair}] Erro: {e}")

        total, pnl = _update_portfolio_state()
        log_portfolio(logger, engine.balance_usd, total, pnl,
                      (pnl / engine.initial_balance) * 100, engine.holdings)
        state["history"].append({"time": now_str, "ts": int(time.time()), "total": round(total, 2)})
        state["history"] = state["history"][-90000:]
        _save_history(state["history"])
        await broadcast(state)
        await asyncio.sleep(CYCLE_INTERVAL)


@app.on_event("startup")
async def startup():
    asyncio.create_task(trading_loop())
