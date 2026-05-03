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

# ── Ciclo e candles ─────────────────────────────────────────────
CYCLE_INTERVAL    = 300      # ciclo de 5 minutos
CANDLE_1H         = "ONE_HOUR"
CANDLE_6H         = "SIX_HOUR"
CANDLE_1D         = "ONE_DAY"

# ── Alocação por estratégia ─────────────────────────────────────
STRAT_ALLOC_PCT   = 0.25     # cada estratégia usa até 25% do portfólio
MIN_TRADE_BRL     = 30.0     # mínimo para cobrir fees

# ── Gestão de risco por posição ─────────────────────────────────
INITIAL_SL_PCT    = 3.0      # SL inicial: -3% da entrada
TAKE_PROFIT_PCT   = 20.0     # TP: +20% (crypto tem movimentos amplos)
TRAILING_STOP_PCT = 5.0      # trailing stop: vende se cair 5% do pico
VOL_REDUCE_PCT    = 0.40     # Volatility Guard: reduz 40% se 3d >8%

client = CoinbaseClient(os.getenv("API_KEY"), os.getenv("SECRET_KEY"))
engine = PaperTradingEngine(initial_balance_usd=10000.0)

# ── 4 estratégias independentes ─────────────────────────────────
all_strategies = [
    RSIDivergence(rsi_period=14, lookback=30, swing_size=5),
    SupportResistance(lookback=40, tolerance_pct=0.5),
    BBSqueeze(period=20, std=2.0, squeeze_pct=3.0),
    GoldenCross(short=50, long=200),
]

# Mapa de candles por estratégia
STRAT_CANDLES = {
    "RSI Divergence": CANDLE_6H,
    "S/R Flip":       CANDLE_1H,
    "BB Squeeze":     CANDLE_1H,
    "Golden Cross":   CANDLE_1D,
}

# Guard de risco global
vol_guard    = VolatilityGuard(threshold_pct=8.0, consecutive_days=3)
trend_filter = TrendFilter(period=50)

# ── Estado por slot (estratégia × par) ─────────────────────────
# Slot: {qty, entry_price, peak_price, realized_pnl_usd}
def _empty_slot():
    return {"qty": 0.0, "entry": 0.0, "peak": 0.0, "realized": 0.0}

SLOTS_FILE = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "data", "strategy_slots.json")

def _load_slots() -> dict:
    try:
        if os.path.exists(SLOTS_FILE):
            with open(SLOTS_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    slots = {}
    for s in all_strategies:
        for p in PAIRS:
            slots[f"{s.name}:{p}"] = _empty_slot()
    return slots

def _save_slots(slots: dict):
    try:
        os.makedirs(os.path.dirname(SLOTS_FILE), exist_ok=True)
        with open(SLOTS_FILE, "w") as f:
            json.dump(slots, f, indent=2)
    except Exception:
        pass

strategy_slots = _load_slots()

# Whale — desativada temporariamente (manter código para uso futuro)
# whale_strategy = WhaleStrategy(whale_multiplier=5.0, top_levels=50, dominance_ratio=1.5)

strategies = all_strategies   # 4 estratégias independentes

last_signals: dict = {}   # {f"{pair}:{strat}": signal} evita re-trigger

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
    "slots":     strategy_slots,   # posições por estratégia
    "portfolio": {"usd": engine.balance_usd, "total": engine.balance_usd,
                  "pnl": 0.0, "pnl_pct": 0.0,
                  "initial_balance": engine.initial_balance,
                  "total_fees_usd": engine.total_fees_usd},
    "trades":    _load_trades_from_engine(),
    "feed":      [],
    "history":   _load_history(),
    "cycle":     0,
    "status":    "running",
    "last_update": "",
    "usd_brl":   5.70,
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
    logger.info("Loop independente — 4 estratégias × 25%%, ciclo %ds", CYCLE_INTERVAL)
    while True:
        state["cycle"] += 1
        now_str = datetime.now().strftime("%H:%M:%S")
        state["last_update"] = now_str

        usd_brl = await asyncio.get_event_loop().run_in_executor(None, _fetch_usd_brl)
        state["usd_brl"] = round(usd_brl, 4)
        portfolio_total  = engine.portfolio_value()

        for pair in PAIRS:
            symbol = pair.split("-")[0]
            try:
                ticker = client.get_ticker(pair)
                price  = float(ticker.get("price", 0))
                if not price:
                    continue

                prev_price    = _last_prices.get(pair, 0)
                price_changed = prev_price == 0 or abs(price - prev_price) / prev_price * 100 >= PRICE_CHANGE_THRESHOLD
                _last_prices[pair] = price
                engine.update_price(symbol, price)
                state["prices"][pair] = {
                    "price":          price,
                    "price_pct_chg":  float(ticker.get("price_percentage_change_24h", 0)),
                    "volume_24h":     float(ticker.get("volume_24h", 0)),
                }

                candles_1h = _get_candles(pair, CANDLE_1H, limit=100)
                candles_6h = _get_candles(pair, CANDLE_6H, limit=100)
                candles_1d = _get_candles(pair, CANDLE_1D, limit=250)

                # Macro: tendência e volatilidade
                df_1d      = trend_filter.candles_to_df(candles_1d)
                trend      = trend_filter.analyze(df_1d)
                vol_signal = vol_guard.analyze(df_1d)
                pair_signals = {"Trend": trend, "Vol Guard": vol_signal}

                # ── Volatilidade extrema: fecha todos os slots deste par ───────
                if vol_signal == "SELL":
                    for strat in all_strategies:
                        key  = f"{strat.name}:{pair}"
                        slot = strategy_slots.get(key, _empty_slot())
                        if slot["qty"] > 0:
                            usd = slot["qty"] * price * (1 - 0.006)
                            if engine.sell(symbol, slot["qty"], price, f"vol_guard:{strat.name}"):
                                slot["realized"] += usd - slot["entry"] * slot["qty"]
                                slot["qty"] = 0.0; slot["entry"] = 0.0; slot["peak"] = 0.0
                                _record_trade("SELL", pair, slot["qty"], price, usd, f"vol_guard:{strat.name}")
                    logger.info(f"[{pair}] VOLATILIDADE EXTREMA — slots fechados")

                if not price_changed:
                    continue

                # ── Cada estratégia age de forma independente ─────────────────
                candle_map = {
                    "RSI Divergence": candles_6h,
                    "S/R Flip":       candles_1h,
                    "BB Squeeze":     candles_1h,
                    "Golden Cross":   candles_1d,
                }

                for strat in all_strategies:
                    key  = f"{strat.name}:{pair}"
                    slot = strategy_slots.setdefault(key, _empty_slot())
                    raw  = candle_map.get(strat.name, candles_1h)
                    df   = strat.candles_to_df(raw)
                    signal = strat.analyze(df)
                    pair_signals[strat.name] = signal

                    # Feed ao vivo
                    sig_key = f"{pair}:{strat.name}"
                    if signal != last_signals.get(sig_key):
                        last_signals[sig_key] = signal
                        state["feed"].insert(0, {"time": now_str, "pair": pair,
                            "strategy": strat.name, "signal": signal, "price": price})
                        state["feed"] = state["feed"][:100]

                    # ── Gestão da posição deste slot ──────────────────────────
                    if slot["qty"] > 0:
                        # Atualiza pico (trailing stop)
                        slot["peak"] = max(slot["peak"], price)

                        tp_hit       = price >= slot["entry"] * (1 + TAKE_PROFIT_PCT / 100)
                        sl_hit       = price <= slot["entry"] * (1 - INITIAL_SL_PCT / 100)
                        trailing_hit = price <= slot["peak"]  * (1 - TRAILING_STOP_PCT / 100)

                        reason = None
                        if tp_hit:       reason = f"TP+{TAKE_PROFIT_PCT:.0f}%"
                        elif sl_hit:     reason = f"SL-{INITIAL_SL_PCT:.0f}%"
                        elif trailing_hit: reason = f"TRAILING-{TRAILING_STOP_PCT:.0f}%"

                        if reason:
                            gross   = slot["qty"] * price
                            fee     = gross * 0.006
                            net_usd = gross - fee
                            if engine.sell(symbol, slot["qty"], price, f"{strat.name}:{reason}"):
                                pnl_trade = net_usd - (slot["entry"] * slot["qty"])
                                slot["realized"] += pnl_trade
                                logger.info(f"[{pair}][{strat.name}] {reason} — P&L: ${pnl_trade:+.2f}")
                                _record_trade("SELL", pair, slot["qty"], price, net_usd, f"{strat.name}:{reason}")
                                slot["qty"] = 0.0; slot["entry"] = 0.0; slot["peak"] = 0.0

                    # ── Compra: sinal BUY + slot vazio + uptrend ─────────────
                    elif signal == "BUY" and trend == "BUY" and slot["qty"] == 0:
                        alloc_usd = portfolio_total * STRAT_ALLOC_PCT
                        min_usd   = MIN_TRADE_BRL / usd_brl
                        if alloc_usd < min_usd or engine.balance_usd < alloc_usd * 1.01:
                            logger.debug(f"[{pair}][{strat.name}] BUY negado — saldo insuf.")
                        else:
                            qty = alloc_usd / price
                            if engine.buy(symbol, alloc_usd, price, strat.name):
                                slot["qty"]   = qty
                                slot["entry"] = price
                                slot["peak"]  = price
                                _record_trade("BUY", pair, qty, price, alloc_usd, strat.name)
                                logger.info(f"[{pair}][{strat.name}] BUY ${alloc_usd:.2f} @ ${price:,.2f}")

                # Atualiza P&L não realizado dos slots
                for strat in all_strategies:
                    key  = f"{strat.name}:{pair}"
                    slot = strategy_slots.get(key, _empty_slot())
                    if slot["qty"] > 0:
                        slot["unrealized"] = (price - slot["entry"]) * slot["qty"]
                    else:
                        slot["unrealized"] = 0.0

                rsi_val     = get_rsi_value(candles_1h)
                entry_price = engine.entry_prices.get(symbol)
                change_pct  = ((price - entry_price) / entry_price * 100) if entry_price else None
                state["signals"][pair] = {
                    "strategies":  pair_signals,
                    "trend":       trend,
                    "vol_guard":   vol_signal,
                    "rsi":         rsi_val,
                    "entry_price": round(entry_price, 2) if entry_price else None,
                    "change_pct":  round(change_pct, 2) if change_pct is not None else None,
                    "sl_level":    round(entry_price * (1 - INITIAL_SL_PCT / 100), 2) if entry_price else None,
                    "tp_level":    round(entry_price * (1 + TAKE_PROFIT_PCT / 100), 2) if entry_price else None,
                }
                _save_slots(strategy_slots)
                state["slots"] = strategy_slots
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
