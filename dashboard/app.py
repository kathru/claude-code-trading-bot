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
from strategies.rsi_divergence import RSIDivergence
from strategies.support_resistance import SupportResistance
from strategies.bb_squeeze import BBSqueeze
from strategies.golden_cross import GoldenCross
from strategies.volatility_guard import VolatilityGuard
from strategies.trend_filter import TrendFilter
# Importações preservadas (desativadas mas mantidas para uso futuro)
# from strategies.ma_crossover import MACrossoverStrategy
# from strategies.rsi import RSIStrategy
# from strategies.scalping import ScalpingStrategy
# from strategies.whale import WhaleStrategy
from logger import setup_logger, log_cycle, log_trade, log_portfolio
from notifier import notify_trade

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), "code.env"))

app = FastAPI()
HTML_FILE    = os.path.join(os.path.dirname(__file__), "templates", "index.html")
STATIC_DIR   = os.path.join(os.path.dirname(__file__), "static")

from fastapi.staticfiles import StaticFiles
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
HISTORY_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "portfolio_history.json")


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


# ── Cache de candles por par ──────────────────────────────────────
CANDLE_TTL = 840             # 14 min — candle de 1h só muda a cada hora
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


# ── Cache de preço anterior para threshold de mudança ────────────
_last_prices: dict = {}      # {pair: float}
PRICE_CHANGE_THRESHOLD = 0.05  # % mínimo para re-analisar estratégias


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
    except Exception as e:
        pass

PAIRS = ["BTC-USD", "ETH-USD"]

# ── Tamanho de trade ────────────────────────────────────────────
TRADE_MAX_BRL     = 250.0    # máximo por operação em R$
TRADE_MIN_BRL     = 50.0     # mínimo (fee deve valer a pena)
MAX_ALLOC_PCT     = 0.25     # máximo 25% do portfólio por par (regra 10-30%)

# ── Ciclo e candles ─────────────────────────────────────────────
CYCLE_INTERVAL    = 300      # ciclo de 5 minutos
CANDLE_1H         = "ONE_HOUR"
CANDLE_6H         = "SIX_HOUR"    # para RSI Divergence
CANDLE_1D         = "ONE_DAY"     # para Golden Cross e Volatility Guard

# ── Proteção de posição ─────────────────────────────────────────
STOP_LOSS_PCT     = 4.0      # SL: -4%
TAKE_PROFIT_PCT   = 8.0      # TP: +8%
DOUBLE_SELL_PCT   = 0.50     # Double Rule: vende 50% se dobrar
VOL_REDUCE_PCT    = 0.40     # Volatility Guard: reduz 40% se 3d >8%

# ── Consenso ────────────────────────────────────────────────────
TECH_BUY_CONSENSUS = 2       # mínimo de estratégias técnicas para BUY


def calc_trade_brl(conviction: float) -> float:
    """
    Calcula o valor do trade baseado na convicção do sinal (0.0 a 1.0).
    conviction = 1.0 → TRADE_MAX_BRL (R$250)
    conviction = 0.0 → TRADE_MIN_BRL (R$25)
    """
    trade = TRADE_MIN_BRL + conviction * (TRADE_MAX_BRL - TRADE_MIN_BRL)
    return round(max(TRADE_MIN_BRL, min(trade, TRADE_MAX_BRL)), 2)

client = CoinbaseClient(os.getenv("API_KEY"), os.getenv("SECRET_KEY"))
engine = PaperTradingEngine(initial_balance_usd=10000.0)

# ── Filtro macro de tendência (MA50 diário) ─────────────────────
trend_filter = TrendFilter(period=50)

# ── Estratégias de entrada — consenso 2/3 ───────────────────────
tech_strategies = [
    RSIDivergence(rsi_period=14, lookback=30, swing_size=5),    # 6H
    SupportResistance(lookback=40, tolerance_pct=0.5),          # 1H
    BBSqueeze(period=20, std=2.0, squeeze_pct=3.0),             # 1H
]

# Golden Cross — usado apenas como confirmação extra (não vota)
golden_cross = GoldenCross(short=50, long=200)                  # 1D

# ── Guard de risco (pode forçar redução de posição) ─────────────
vol_guard = VolatilityGuard(threshold_pct=8.0, consecutive_days=3)  # 1D

# Whale — desativada temporariamente (manter código para uso futuro)
# whale_strategy = WhaleStrategy(whale_multiplier=5.0, top_levels=50, dominance_ratio=1.5)

strategies = tech_strategies   # whale removida do ciclo ativo

# Controle de sinal anterior por (pair, strategy) para evitar re-execução no mesmo sinal
last_signals: dict = {}
# last_whale_run: float = 0.0   # reservado para quando whale for reativada
entry_times: dict = {}        # {symbol: timestamp de entrada} para hold mínimo

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
    "prices": {},
    "signals": {},
    "portfolio": {"usd": engine.balance_usd, "total": engine.balance_usd, "pnl": 0.0, "pnl_pct": 0.0, "initial_balance": engine.initial_balance, "total_fees_usd": engine.total_fees_usd},
    "trades": _load_trades_from_engine(),
    "feed": [],
    "history": _load_history(),
    "cycle": 0,
    "status": "running",
    "last_update": "",
    "usd_brl":    5.70,
    "trade_max_brl": TRADE_MAX_BRL,
    "trade_min_brl": TRADE_MIN_BRL,
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
async def manual_buy(pair: str, brl: float = 250.0):
    symbol = pair.split("-")[0]
    ticker = client.get_ticker(pair)
    price = float(ticker.get("price", 0))
    if not price:
        return {"ok": False, "error": "Preço indisponível"}
    usd = brl / state["usd_brl"]
    qty = usd / price
    ok = engine.buy(symbol, usd, price, "manual")
    if ok:
        engine.update_price(symbol, price)
        log_trade(logger, "BUY", pair, qty, price, usd, "manual")
        notify_trade("BUY", pair, qty, price, usd)
        state["trades"].insert(0, {
            "time": datetime.now().strftime("%H:%M:%S"),
            "side": "BUY", "pair": pair, "price": price, "usd": usd,
        })
        state["trades"] = state["trades"][:50]
        _update_portfolio_state()
        await broadcast(state)
        return {"ok": True, "qty": qty, "price": price, "usd": usd}
    return {"ok": False, "error": "Saldo insuficiente"}


@app.post("/trade/sell")
async def manual_sell(pair: str):
    symbol = pair.split("-")[0]
    held = engine.holdings.get(symbol, 0)
    if held <= 0:
        return {"ok": False, "error": f"Sem {symbol} para vender"}
    ticker = client.get_ticker(pair)
    price = float(ticker.get("price", 0))
    if not price:
        return {"ok": False, "error": "Preço indisponível"}
    usd = held * price
    ok = engine.sell(symbol, held, price, "manual")
    if ok:
        log_trade(logger, "SELL", pair, held, price, usd, "manual")
        notify_trade("SELL", pair, held, price, usd)
        state["trades"].insert(0, {
            "time": datetime.now().strftime("%H:%M:%S"),
            "side": "SELL", "pair": pair, "price": price, "usd": usd,
        })
        state["trades"] = state["trades"][:50]
        _update_portfolio_state()
        await broadcast(state)
        return {"ok": True, "qty": held, "price": price, "usd": usd}
    return {"ok": False, "error": "Falha na venda"}


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
    if side == "BUY":
        entry_times[symbol] = time.time()
    elif side == "SELL" and symbol in entry_times:
        del entry_times[symbol]


async def trading_loop():
    global last_whale_run
    logger.info("Loop iniciado — 1H/6H/1D, ciclo %ds", CYCLE_INTERVAL)
    while True:
        state["cycle"] += 1
        now_str = datetime.now().strftime("%H:%M:%S")
        state["last_update"] = now_str

        # Cotação USD/BRL — usa cache (30min TTL, sem chamada extra)
        usd_brl = await asyncio.get_event_loop().run_in_executor(None, _fetch_usd_brl)
        state["usd_brl"] = round(usd_brl, 4)

        for pair in PAIRS:
            symbol = pair.split("-")[0]
            try:
                ticker = client.get_ticker(pair)
                price  = float(ticker.get("price", 0))
                if not price:
                    continue

                # Skip de análise se preço não mudou o suficiente
                prev_price = _last_prices.get(pair, 0)
                price_changed = prev_price == 0 or abs(price - prev_price) / prev_price * 100 >= PRICE_CHANGE_THRESHOLD
                _last_prices[pair] = price

                engine.update_price(symbol, price)
                state["prices"][pair] = {
                    "price": price,
                    "price_pct_chg": float(ticker.get("price_percentage_change_24h", 0)),
                    "volume_24h": float(ticker.get("volume_24h", 0)),
                }

                # ── Stop Loss / Take Profit ──────────────────────────
                entry = engine.entry_prices.get(symbol)
                held  = engine.holdings.get(symbol, 0)
                if entry and held > 0:
                    change_pct = (price - entry) / entry * 100
                    sl_trigger = change_pct <= -STOP_LOSS_PCT
                    tp_trigger = change_pct >= TAKE_PROFIT_PCT
                    reason = None
                    if sl_trigger:
                        reason = f"STOP_LOSS ({change_pct:.2f}% ≤ -{STOP_LOSS_PCT}%)"
                    elif tp_trigger:
                        reason = f"TAKE_PROFIT ({change_pct:.2f}% ≥ +{TAKE_PROFIT_PCT}%)"
                    if reason:
                        usd = held * price
                        if engine.sell(symbol, held, price, reason):
                            logger.info(f"[{pair}] {reason} acionado — vendeu {held:.6f} @ ${price:,.2f}")
                            _record_trade("SELL", pair, held, price, usd, reason)
                            state["feed"].insert(0, {
                                "time": now_str, "pair": pair,
                                "strategy": reason, "signal": "SELL", "price": price,
                            })
                            state["feed"] = state["feed"][:100]

                # Candles multi-timeframe via cache
                candles_1h = _get_candles(pair, CANDLE_1H, limit=100)
                candles_6h = _get_candles(pair, CANDLE_6H, limit=100)
                candles_1d = _get_candles(pair, CANDLE_1D, limit=250)

                if not price_changed:
                    state["prices"][pair] = {
                        "price": price,
                        "price_pct_chg": float(ticker.get("price_percentage_change_24h", 0)),
                        "volume_24h":    float(ticker.get("volume_24h", 0)),
                    }
                    continue

                pair_signals = {}

                # ── Filtro macro: tendência MA50 diário ───────────────────────
                df_1d = trend_filter.candles_to_df(candles_1d)
                trend = trend_filter.analyze(df_1d)
                pair_signals["Trend"] = trend

                # ── Volatility Guard (diário) — proteção de risco ─────────────
                vol_signal = vol_guard.analyze(df_1d)
                pair_signals["Vol Guard"] = vol_signal

                if vol_signal == "SELL":
                    held = engine.holdings.get(symbol, 0)
                    if held > 0:
                        reduce_qty = held * VOL_REDUCE_PCT
                        usd = reduce_qty * price
                        logger.info(f"[{pair}] VOLATILIDADE EXTREMA — reduzindo {VOL_REDUCE_PCT:.0%}")
                        if engine.sell(symbol, reduce_qty, price, "vol_guard"):
                            _record_trade("SELL", pair, reduce_qty, price, usd, "vol_guard")

                # ── Double Rule — vende 50% se posição dobrou ─────────────────
                entry_p = engine.entry_prices.get(symbol)
                held    = engine.holdings.get(symbol, 0)
                if entry_p and held > 0 and price >= entry_p * 2.0:
                    sell_qty = held * DOUBLE_SELL_PCT
                    usd = sell_qty * price
                    logger.info(f"[{pair}] DOUBLE RULE — posição dobrou! Vendendo {DOUBLE_SELL_PCT:.0%}")
                    if engine.sell(symbol, sell_qty, price, "double_rule"):
                        _record_trade("SELL", pair, sell_qty, price, usd, "double_rule")

                # ── Estratégias técnicas de entrada ───────────────────────────
                tech_votes = {"BUY": 0, "SELL": 0, "HOLD": 0}
                candle_map = {
                    "RSI Divergence": candles_6h,
                    "S/R Flip":       candles_1h,
                    "BB Squeeze":     candles_1h,
                }
                for strategy in tech_strategies:
                    raw = candle_map.get(strategy.name, candles_1h)
                    df  = strategy.candles_to_df(raw)
                    signal = strategy.analyze(df)
                    pair_signals[strategy.name] = signal
                    tech_votes[signal] += 1
                    key = f"{pair}:{strategy.name}"
                    if signal != last_signals.get(key):
                        last_signals[key] = signal
                        state["feed"].insert(0, {"time": now_str, "pair": pair,
                            "strategy": strategy.name, "signal": signal, "price": price})
                        state["feed"] = state["feed"][:100]

                tech_decision = max(tech_votes, key=tech_votes.get)

                # Golden Cross — confirmação extra (não vota, mas registra no feed)
                gc_df     = golden_cross.candles_to_df(candles_1d)
                gc_signal = golden_cross.analyze(gc_df)
                pair_signals["Golden Cross"] = gc_signal
                key_gc = f"{pair}:Golden Cross"
                if gc_signal != last_signals.get(key_gc):
                    last_signals[key_gc] = gc_signal
                    state["feed"].insert(0, {"time": now_str, "pair": pair,
                        "strategy": "Golden Cross", "signal": gc_signal, "price": price})
                    state["feed"] = state["feed"][:100]

                # BUY: consenso 2/3 + uptrend MA50 + Golden Cross confirma + alocação ok
                portfolio_total = engine.portfolio_value()
                held_value = engine.holdings.get(symbol, 0) * price
                alloc_pct  = held_value / portfolio_total if portfolio_total > 0 else 0

                if (tech_votes["BUY"] >= TECH_BUY_CONSENSUS
                        and trend == "BUY"
                        and gc_signal == "BUY"
                        and alloc_pct < MAX_ALLOC_PCT):
                    conviction = tech_votes["BUY"] / len(tech_strategies)
                    # Limita pelo espaço de alocação disponível
                    max_trade_brl = (MAX_ALLOC_PCT - alloc_pct) * portfolio_total * state["usd_brl"]
                    trade_brl = min(calc_trade_brl(conviction), max_trade_brl)
                    trade_usd = trade_brl / state["usd_brl"]
                    qty = trade_usd / price
                    logger.info(f"[{pair}] COMPRA {tech_votes['BUY']}/{len(tech_strategies)} → R${trade_brl:.0f} (alloc {alloc_pct:.0%}→{alloc_pct+trade_brl/portfolio_total/state['usd_brl']:.0%})")
                    if engine.buy(symbol, trade_usd, price, "técnico"):
                        _record_trade("BUY", pair, qty, price, trade_usd, "técnico")
                    else:
                        logger.warning(f"[{pair}] BUY negado — saldo ${engine.balance_usd:.2f}")

                # ── Bloco whale: DESATIVADO (código preservado para reativação) ──
                # Para reativar: descomentar bloco whale em app.py e HTML
                # whale_signal = whale_strategy.analyze_book(order_book) ...

                rsi_val    = get_rsi_value(candles_1h)
                entry_price = engine.entry_prices.get(symbol)
                change_pct  = ((price - entry_price) / entry_price * 100) if entry_price else None
                state["signals"][pair] = {
                    "strategies":   pair_signals,
                    "tech_votes":   tech_votes,
                    "tech_decision": tech_decision,
                    "trend":        trend,
                    "vol_guard":    vol_signal,
                    "rsi":          rsi_val,
                    "entry_price": round(entry_price, 2) if entry_price else None,
                    "change_pct":  round(change_pct, 2) if change_pct is not None else None,
                    "sl_level":    round(entry_price * (1 - STOP_LOSS_PCT/100), 2) if entry_price else None,
                    "tp_level":    round(entry_price * (1 + TAKE_PROFIT_PCT/100), 2) if entry_price else None,
                }
                log_cycle(logger, state["cycle"], pair, price, pair_signals, tech_decision)

            except Exception as e:
                state["signals"][pair] = {"error": str(e)}
                logger.error(f"[{pair}] Erro: {e}")

        # Whale desativada — placeholder para reativação futura
        # if (time.time() - last_whale_run) >= WHALE_INTERVAL:
        #     last_whale_run = time.time()

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
