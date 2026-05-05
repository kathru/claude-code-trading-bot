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

PAIRS = ["BTC-USD", "ETH-USD"]

# ── Ciclo e candles ─────────────────────────────────────────────
CYCLE_INTERVAL    = 90       # ciclo de 90s — 3.3× mais reativo (era 300s)
CANDLE_30M        = "THIRTY_MINUTE"  # Donchian, Stoch
CANDLE_1H         = "ONE_HOUR"       # EMA Pullback, MACD
CANDLE_6H         = "SIX_HOUR"
CANDLE_1D         = "ONE_DAY"        # Trend, VolGuard

# ── Alocação agressiva por estratégia ───────────────────────────
TRADE_MAX_BRL     = 600.0    # limite máximo por operação em R$ (era 250 → 2.4× mais)
STRAT_ALLOC_PCT   = 0.25     # cada estratégia usa 25% → R$150/trade
MIN_TRADE_BRL     = 10.0

# ── Gestão de risco — modo agressivo (R/R = 6:1) ────────────────
INITIAL_SL_PCT       = 5.0   # SL: -5% (era -3%) — evita whipsaws de wick
TAKE_PROFIT_PCT      = 5.0   # TP: +5% — igual ao SL (R/R 1:1)
TRAILING_STOP_PCT    = 8.0   # trailing: -8% do pico (era -5%) — mais espaço
TRAILING_ACTIVATE_PCT = 6.0  # trailing só ativa após +6% (era +2%)
SL_COOLDOWN_CYCLES   = 2     # após SL, espera 2 ciclos antes de re-entrar
VOL_REDUCE_PCT       = 0.40

# ── Pyramid (scale-in em posição lucrativa) ──────────────────────
PYRAMID_MAX          = 2     # máx. 2 adições por slot (3 entradas no total)
PYRAMID_MIN_GAIN_PCT = 0.5   # só adiciona se posição estiver ≥ +0.5% no lucro
PYRAMID_SIZE_PCT     = 0.50  # cada pyramid = 50% da alocação inicial (R$75)

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

# ── Cooldown anti-whipsaw após SL ────────────────────────────────
sl_cooldowns: dict = {}   # {f"{strat}:{pair}": cycles_remaining}

# ── Estado por slot (estratégia × par) ─────────────────────────
# Slot: {qty, entry_price, peak_price, realized_pnl_usd}
def _empty_slot():
    return {"qty": 0.0, "entry": 0.0, "peak": 0.0, "realized": 0.0, "pyramids": 0}

SLOTS_FILE = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "data", "strategy_slots.json")

def _load_slots() -> dict:
    slots = {}
    for s in all_strategies:
        for p in PAIRS:
            slots[f"{s.name}:{p}"] = _empty_slot()
    try:
        if os.path.exists(SLOTS_FILE):
            saved = json.load(open(SLOTS_FILE))
            for k, v in saved.items():
                if k in slots:   # só restaura chaves válidas
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
    "cycle":     _current_cycle(),
    "status":        "running",
    "last_update":   "",
    "cycle_start_ts": 0,        # timestamp Unix do início do ciclo atual
    "cycle_interval": CYCLE_INTERVAL,
    "usd_brl":       5.70,
    "trade_max_brl": TRADE_MAX_BRL,
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
    # Registra no slot "manual" para ter TP/SL
    slot_key = f"manual:{pair}"
    strategy_slots[slot_key] = {"qty": qty, "entry": price, "peak": price,
                                 "realized": 0.0, "unrealized": 0.0}
    _save_slots(strategy_slots)
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
    # Venda total: zera todos os slots deste par
    if sell_qty >= held:
        for key in list(strategy_slots.keys()):
            if key.endswith(f":{pair}"):
                strategy_slots[key]["qty"]   = 0.0
                strategy_slots[key]["entry"] = 0.0
                strategy_slots[key]["peak"]  = 0.0
    _save_slots(strategy_slots)
    _record_trade("SELL", pair, sell_qty, price, usd, "manual")
    _update_portfolio_state()
    await broadcast(state)
    return {"ok": True, "qty": sell_qty, "price": price, "usd": usd}


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
        portfolio_total  = engine.portfolio_value()

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

                # ── Volatilidade extrema: fecha todos os slots e PULA ciclo ──
                if vol_signal == "SELL":
                    for strat in all_strategies:
                        key  = f"{strat.name}:{pair}"
                        slot = strategy_slots.get(key, _empty_slot())
                        if slot["qty"] > 0:
                            close_qty = slot["qty"]
                            usd = close_qty * price * (1 - 0.006)
                            if engine.sell(symbol, close_qty, price, f"vol_guard:{strat.name}"):
                                slot["realized"] += usd - slot["entry"] * close_qty
                                slot["qty"] = 0.0; slot["entry"] = 0.0; slot["peak"] = 0.0
                                _record_trade("SELL", pair, close_qty, price, usd, f"vol_guard:{strat.name}")
                            else:
                                # Sync forçado se engine negou (floating point extremo)
                                slot["qty"] = 0.0; slot["entry"] = 0.0; slot["peak"] = 0.0
                    _save_slots(strategy_slots)
                    logger.info(f"[{pair}] VOLATILIDADE EXTREMA — slots fechados, ciclo pausado")
                    # ← Não executa estratégias neste ciclo para evitar re-abertura imediata

                else:
                  # ── Cada estratégia age de forma independente ─────────────
                  # Análise executada todo ciclo (cache de 240s nas candles evita
                  # chamadas excessivas à API; pandas é rápido o suficiente).
                  candles_30m = _get_candles(pair, CANDLE_30M, limit=250)
                  candle_map = {
                      "Donchian Breakout": candles_30m,
                      "EMA Pullback":      candles_1h,
                      "MACD Momentum":     candles_1h,
                      "Stoch Bounce":      candles_30m,
                  }

                  for strat in all_strategies:
                    key  = f"{strat.name}:{pair}"
                    slot = strategy_slots.setdefault(key, _empty_slot())

                    raw    = candle_map.get(strat.name, candles_1h)
                    df     = strat.candles_to_df(raw)
                    signal = strat.analyze(df)
                    pair_signals[strat.name] = signal

                    # Feed ao vivo — registra quando sinal muda, com status de execução
                    sig_key    = f"{pair}:{strat.name}"
                    new_feed   = None
                    if signal != last_signals.get(sig_key):
                        last_signals[sig_key] = signal
                        new_feed = {
                            "time":     now_str,
                            "cycle":    state["cycle"],
                            "pair":     pair,
                            "strategy": strat.name,
                            "signal":   signal,
                            "price":    price,
                            "executed": False,
                            "note":     "",
                        }
                        state["feed"].insert(0, new_feed)
                        state["feed"] = state["feed"][:100]

                    # ── Gestão da posição deste slot ─────────────────────────
                    if slot["qty"] > 0:
                        slot["peak"] = max(slot["peak"], price)
                        gain_pct    = (price - slot["entry"]) / slot["entry"] * 100

                        tp_hit       = price >= slot["entry"] * (1 + TAKE_PROFIT_PCT / 100)
                        sl_hit       = price <= slot["entry"] * (1 - INITIAL_SL_PCT / 100)
                        trailing_active = gain_pct >= TRAILING_ACTIVATE_PCT
                        trailing_hit    = trailing_active and price <= slot["peak"] * (1 - TRAILING_STOP_PCT / 100)

                        reason = None
                        if tp_hit:         reason = f"TP+{TAKE_PROFIT_PCT:.0f}%"
                        elif sl_hit:       reason = f"SL-{INITIAL_SL_PCT:.0f}%"
                        elif trailing_hit: reason = f"TRAILING-{TRAILING_STOP_PCT:.0f}%"

                        if reason:
                            close_qty = slot["qty"]
                            net_usd   = close_qty * price * (1 - 0.006)
                            if engine.sell(symbol, close_qty, price, f"{strat.name}:{reason}"):
                                pnl_trade = net_usd - (slot["entry"] * close_qty)
                                slot["realized"] += pnl_trade
                                logger.info(f"[{pair}][{strat.name}] {reason} — P&L: ${pnl_trade:+.2f}")
                                _record_trade("SELL", pair, close_qty, price, net_usd, f"{strat.name}:{reason}")
                                slot["qty"] = 0.0; slot["entry"] = 0.0; slot["peak"] = 0.0; slot["pyramids"] = 0
                                if "SL-" in reason:
                                    sl_cooldowns[key] = SL_COOLDOWN_CYCLES
                                if new_feed: new_feed.update({"executed": True, "note": reason})
                            else:
                                # Engine negou — sync forçado para evitar estado fantasma
                                logger.warning(f"[{pair}][{strat.name}] SELL negado pela engine — sync forçado")
                                slot["qty"] = 0.0; slot["entry"] = 0.0; slot["peak"] = 0.0; slot["pyramids"] = 0

                        elif signal == "SELL":
                            close_qty = slot["qty"]
                            net_usd   = close_qty * price * (1 - 0.006)
                            if engine.sell(symbol, close_qty, price, f"{strat.name}:SIGNAL"):
                                pnl_trade = net_usd - (slot["entry"] * close_qty)
                                slot["realized"] += pnl_trade
                                logger.info(f"[{pair}][{strat.name}] SELL SIGNAL — P&L: ${pnl_trade:+.2f}")
                                _record_trade("SELL", pair, close_qty, price, net_usd, f"{strat.name}:SIGNAL")
                                slot["qty"] = 0.0; slot["entry"] = 0.0; slot["peak"] = 0.0; slot["pyramids"] = 0
                                if new_feed: new_feed.update({"executed": True, "note": "sinal técnico"})
                            else:
                                logger.warning(f"[{pair}][{strat.name}] SELL SIGNAL negado — sync forçado")
                                slot["qty"] = 0.0; slot["entry"] = 0.0; slot["peak"] = 0.0; slot["pyramids"] = 0

                        elif signal == "BUY":
                            # ── Pyramid: adiciona à posição se em lucro e abaixo do limite ──
                            pyramids_done = slot.get("pyramids", 0)
                            if pyramids_done >= PYRAMID_MAX:
                                if new_feed: new_feed["note"] = f"em posição · max pyramids ({PYRAMID_MAX})"
                            elif gain_pct < PYRAMID_MIN_GAIN_PCT:
                                if new_feed: new_feed["note"] = f"em posição · aguard. +{PYRAMID_MIN_GAIN_PCT}%"
                            else:
                                pyr_brl = TRADE_MAX_BRL * STRAT_ALLOC_PCT * PYRAMID_SIZE_PCT  # R$75
                                pyr_usd = pyr_brl / usd_brl
                                if engine.balance_usd < pyr_usd * 1.006:
                                    if new_feed: new_feed["note"] = "pyramid · saldo insuf."
                                    logger.info(f"[{pair}][{strat.name}] Pyramid bloqueado — saldo insuficiente")
                                else:
                                    add_qty = pyr_usd / price
                                    if engine.buy(symbol, pyr_usd, price, f"{strat.name}:pyramid"):
                                        total_qty      = slot["qty"] + add_qty
                                        slot["entry"]  = (slot["qty"] * slot["entry"] + add_qty * price) / total_qty
                                        slot["qty"]    = total_qty
                                        slot["peak"]   = max(slot["peak"], price)
                                        slot["pyramids"] = pyramids_done + 1
                                        _record_trade("BUY", pair, add_qty, price, pyr_usd, f"{strat.name}:pyramid{pyramids_done+1}")
                                        logger.info(f"[{pair}][{strat.name}] 📈 PYRAMID #{pyramids_done+1} R${pyr_brl:.0f} @ ${price:,.2f} (gain {gain_pct:.1f}%)")
                                        state["feed"].insert(0, {
                                            "time": now_str, "cycle": state["cycle"],
                                            "pair": pair, "strategy": strat.name,
                                            "signal": "BUY", "price": price,
                                            "executed": True,
                                            "note": f"pyramid #{pyramids_done+1} R${pyr_brl:.0f}",
                                        })
                                        state["feed"] = state["feed"][:100]
                        else:
                            # Em posição, sinal HOLD — aguardando saída
                            if new_feed: new_feed["note"] = "em posição"

                    # ── Compra: sinal BUY + slot vazio + sem cooldown ─────────
                    elif signal == "BUY" and slot["qty"] == 0:
                        cooldown = sl_cooldowns.get(key, 0)
                        if cooldown > 0:
                            sl_cooldowns[key] = cooldown - 1
                            logger.info(f"[{pair}][{strat.name}] BUY ignorado — cooldown ({cooldown})")
                            if new_feed: new_feed["note"] = f"cooldown {cooldown}c"
                        else:
                            alloc_brl = TRADE_MAX_BRL * STRAT_ALLOC_PCT
                            alloc_usd = alloc_brl / usd_brl
                            if engine.balance_usd < alloc_usd * 1.006:
                                logger.info(f"[{pair}][{strat.name}] BUY negado — saldo US${engine.balance_usd:.2f} insuf.")
                                if new_feed: new_feed["note"] = "saldo insuf."
                            else:
                                qty = alloc_usd / price
                                if engine.buy(symbol, alloc_usd, price, strat.name):
                                    slot["qty"]   = qty
                                    slot["entry"] = price
                                    slot["peak"]  = price
                                    _record_trade("BUY", pair, qty, price, alloc_usd, strat.name)
                                    logger.info(f"[{pair}][{strat.name}] ✅ BUY R${alloc_brl:.2f} @ US${price:,.2f}")
                                    if new_feed: new_feed.update({"executed": True, "note": f"R${alloc_brl:.0f}"})

                # ── Slot manual: SL/TP/Trailing igual às estratégias ─────────
                manual_slot = strategy_slots.get(f"manual:{pair}")
                if manual_slot and manual_slot.get("qty", 0) > 0:
                    manual_slot["peak"] = max(manual_slot["peak"], price)
                    gain_pct     = (price - manual_slot["entry"]) / manual_slot["entry"] * 100
                    tp_hit       = price >= manual_slot["entry"] * (1 + TAKE_PROFIT_PCT / 100)
                    sl_hit       = price <= manual_slot["entry"] * (1 - INITIAL_SL_PCT / 100)
                    trailing_act = gain_pct >= TRAILING_ACTIVATE_PCT
                    trailing_hit = trailing_act and price <= manual_slot["peak"] * (1 - TRAILING_STOP_PCT / 100)
                    reason = None
                    if tp_hit:         reason = f"TP+{TAKE_PROFIT_PCT:.0f}%"
                    elif sl_hit:       reason = f"SL-{INITIAL_SL_PCT:.0f}%"
                    elif trailing_hit: reason = f"TRAILING-{TRAILING_STOP_PCT:.0f}%"
                    if reason:
                        close_qty = manual_slot["qty"]
                        net_usd   = close_qty * price * (1 - 0.006)
                        if engine.sell(symbol, close_qty, price, f"manual:{reason}"):
                            manual_slot["realized"] += net_usd - manual_slot["entry"] * close_qty
                            manual_slot["qty"] = 0.0; manual_slot["entry"] = 0.0; manual_slot["peak"] = 0.0
                            _record_trade("SELL", pair, close_qty, price, net_usd, f"manual:{reason}")
                            logger.info(f"[{pair}][manual] {reason} acionado @ ${price:,.2f}")
                    else:
                        manual_slot["unrealized"] = (price - manual_slot["entry"]) * manual_slot["qty"]

                # Atualiza P&L não realizado dos slots de estratégia
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
