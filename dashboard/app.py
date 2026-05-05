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

# ── Modelo de consenso ──────────────────────────────────────────
TRADE_AMOUNT_BRL  = 500.0    # valor por trade (único por par)
CONSENSUS_BUY_MIN = 2        # nº mínimo de estratégias para BUY (threshold)
# SELL: qualquer 1 estratégia já fecha a posição

# ── Gestão de risco ──────────────────────────────────────────────
INITIAL_SL_PCT       = 5.0   # SL: -5%
TAKE_PROFIT_PCT      = 5.0   # TP: +5% (igual ao SL — R/R 1:1)
TRAILING_STOP_PCT    = 8.0   # trailing: -8% do pico
TRAILING_ACTIVATE_PCT = 6.0  # trailing só ativa após +6%
SL_COOLDOWN_CYCLES   = 2     # após SL, espera 2 ciclos antes de re-entrar

# ── Pyramid (scale-in em posição lucrativa) ──────────────────────
PYRAMID_MAX          = 2     # máx. 2 adições por posição (3 entradas total)
PYRAMID_MIN_GAIN_PCT = 0.5   # só adiciona se ≥ +0.5% no lucro
PYRAMID_SIZE_PCT     = 0.25  # cada pyramid = 25% do trade = R$125

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
sl_cooldowns: dict = {}   # {pair: cycles_remaining}

# ── Posição por par (consenso) ───────────────────────────────────
def _empty_position():
    return {"qty": 0.0, "entry": 0.0, "peak": 0.0,
            "realized": 0.0, "unrealized": 0.0, "pyramids": 0}

POSITIONS_FILE = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "data", "positions.json")

def _load_positions() -> dict:
    pos = {p: _empty_position() for p in PAIRS}
    try:
        if os.path.exists(POSITIONS_FILE):
            saved = json.load(open(POSITIONS_FILE))
            for k, v in saved.items():
                if k in pos:
                    pos[k].update(v)
    except Exception:
        pass
    return pos

def _save_positions(pos: dict):
    try:
        os.makedirs(os.path.dirname(POSITIONS_FILE), exist_ok=True)
        with open(POSITIONS_FILE, "w") as f:
            json.dump(pos, f, indent=2)
    except Exception:
        pass

positions = _load_positions()

# ── Slots manuais (trades via botão) ─────────────────────────────
MANUAL_FILE = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "data", "manual_slots.json")

def _load_manual() -> dict:
    slots = {f"manual:{p}": {"qty": 0.0, "entry": 0.0, "peak": 0.0,
                               "realized": 0.0, "unrealized": 0.0} for p in PAIRS}
    try:
        if os.path.exists(MANUAL_FILE):
            saved = json.load(open(MANUAL_FILE))
            slots.update({k: v for k, v in saved.items() if k in slots})
    except Exception:
        pass
    return slots

def _save_manual(slots: dict):
    try:
        os.makedirs(os.path.dirname(MANUAL_FILE), exist_ok=True)
        with open(MANUAL_FILE, "w") as f:
            json.dump(slots, f, indent=2)
    except Exception:
        pass

strategy_slots = _load_manual()   # mantém compatibilidade com endpoints manuais

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
    "positions": positions,         # posições consenso por par
    "slots":     strategy_slots,   # slots manuais (compat. botões buy/sell)
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
    "usd_brl":          5.70,
    "trade_amount_brl": TRADE_AMOUNT_BRL,
    "consensus_min":    CONSENSUS_BUY_MIN,
    "positions_detail": {},
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
    # Venda total: zera slot manual e posição de consenso deste par
    if sell_qty >= held:
        slot_k = f"manual:{pair}"
        if slot_k in strategy_slots:
            strategy_slots[slot_k]["qty"]   = 0.0
            strategy_slots[slot_k]["entry"] = 0.0
            strategy_slots[slot_k]["peak"]  = 0.0
        if pair in positions:
            positions[pair]["qty"]   = 0.0
            positions[pair]["entry"] = 0.0
            positions[pair]["peak"]  = 0.0
            positions[pair]["pyramids"] = 0
        _save_positions(positions)
    _save_manual(strategy_slots)
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
                  # ── PASSO 1: coleta sinais de todas as estratégias ────────
                  candles_30m = _get_candles(pair, CANDLE_30M, limit=250)
                  candle_map  = {
                      "Donchian Breakout": candles_30m,
                      "EMA Pullback":      candles_1h,
                      "MACD Momentum":     candles_1h,
                      "Stoch Bounce":      candles_30m,
                  }
                  buy_score   = 0
                  sell_score  = 0
                  sell_strats = []

                  for strat in all_strategies:
                      raw    = candle_map.get(strat.name, candles_1h)
                      df     = strat.candles_to_df(raw)
                      signal = strat.analyze(df)
                      pair_signals[strat.name] = signal
                      if signal == "BUY":
                          buy_score  += 1
                      elif signal == "SELL":
                          sell_score += 1
                          sell_strats.append(strat.name)

                      # Feed — registra quando sinal muda
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
                              "note":     f"↑{buy_score} ↓{sell_score}",
                          })
                          state["feed"] = state["feed"][:100]

                  pair_signals["buy_score"]  = buy_score
                  pair_signals["sell_score"] = sell_score

                  # ── PASSO 2: execução por consenso (1 posição por par) ───
                  pos = positions[pair]

                  if pos["qty"] > 0:
                      pos["peak"] = max(pos["peak"], price)
                      gain_pct    = (price - pos["entry"]) / pos["entry"] * 100

                      # SL / TP / Trailing
                      tp_hit      = price >= pos["entry"] * (1 + TAKE_PROFIT_PCT   / 100)
                      sl_hit      = price <= pos["entry"] * (1 - INITIAL_SL_PCT    / 100)
                      tr_active   = gain_pct >= TRAILING_ACTIVATE_PCT
                      tr_hit      = tr_active and price <= pos["peak"] * (1 - TRAILING_STOP_PCT / 100)

                      reason = None
                      if tp_hit:  reason = f"TP+{TAKE_PROFIT_PCT:.0f}%"
                      elif sl_hit: reason = f"SL-{INITIAL_SL_PCT:.0f}%"
                      elif tr_hit: reason = f"TRAILING-{TRAILING_STOP_PCT:.0f}%"

                      if reason:
                          close_qty = pos["qty"]
                          net_usd   = close_qty * price * (1 - 0.006)
                          if engine.sell(symbol, close_qty, price, f"consenso:{reason}"):
                              pnl = net_usd - pos["entry"] * close_qty
                              pos["realized"] += pnl
                              logger.info(f"[{pair}] {reason} — P&L: ${pnl:+.2f}")
                              _record_trade("SELL", pair, close_qty, price, net_usd, f"consenso:{reason}")
                              pos["qty"] = 0.0; pos["entry"] = 0.0; pos["peak"] = 0.0; pos["pyramids"] = 0
                              if "SL-" in reason:
                                  sl_cooldowns[pair] = SL_COOLDOWN_CYCLES
                          else:
                              pos["qty"] = 0.0; pos["entry"] = 0.0; pos["peak"] = 0.0; pos["pyramids"] = 0

                      elif sell_score >= 1:
                          # Qualquer estratégia SELL → fecha imediatamente
                          close_qty  = pos["qty"]
                          net_usd    = close_qty * price * (1 - 0.006)
                          sell_label = "+".join(sell_strats[:2])
                          if engine.sell(symbol, close_qty, price, f"consenso:SELL·{sell_label}"):
                              pnl = net_usd - pos["entry"] * close_qty
                              pos["realized"] += pnl
                              logger.info(f"[{pair}] SELL consenso ({sell_strats}) — P&L: ${pnl:+.2f}")
                              _record_trade("SELL", pair, close_qty, price, net_usd,
                                            f"SELL·{sell_score}↓·{sell_label}")
                              pos["qty"] = 0.0; pos["entry"] = 0.0; pos["peak"] = 0.0; pos["pyramids"] = 0
                          else:
                              pos["qty"] = 0.0; pos["entry"] = 0.0; pos["peak"] = 0.0; pos["pyramids"] = 0

                      elif buy_score >= CONSENSUS_BUY_MIN and gain_pct >= PYRAMID_MIN_GAIN_PCT:
                          # Pyramid: score ≥ 2 + posição em lucro
                          pyramids_done = pos.get("pyramids", 0)
                          if pyramids_done < PYRAMID_MAX:
                              pyr_brl = TRADE_AMOUNT_BRL * PYRAMID_SIZE_PCT   # R$125
                              pyr_usd = pyr_brl / usd_brl
                              if engine.balance_usd >= pyr_usd * 1.006:
                                  add_qty = pyr_usd / price
                                  if engine.buy(symbol, pyr_usd, price,
                                                f"consenso:pyramid{pyramids_done+1}·{buy_score}↑"):
                                      total_qty  = pos["qty"] + add_qty
                                      pos["entry"] = (pos["qty"] * pos["entry"] + add_qty * price) / total_qty
                                      pos["qty"]   = total_qty
                                      pos["peak"]  = max(pos["peak"], price)
                                      pos["pyramids"] = pyramids_done + 1
                                      _record_trade("BUY", pair, add_qty, price, pyr_usd,
                                                    f"pyramid{pyramids_done+1}·score{buy_score}")
                                      logger.info(f"[{pair}] 📈 PYRAMID #{pyramids_done+1} "
                                                  f"R${pyr_brl:.0f} @ ${price:,.2f} "
                                                  f"(score {buy_score}/4, gain {gain_pct:.1f}%)")
                                      state["feed"].insert(0, {
                                          "time": now_str, "cycle": state["cycle"],
                                          "pair": pair, "strategy": "Consenso",
                                          "signal": "BUY", "price": price,
                                          "executed": True,
                                          "note": f"pyramid #{pyramids_done+1} R${pyr_brl:.0f} score {buy_score}/4",
                                      })
                                      state["feed"] = state["feed"][:100]

                  elif buy_score >= CONSENSUS_BUY_MIN:
                      # Abre posição: score ≥ 2 estratégias de BUY
                      cooldown = sl_cooldowns.get(pair, 0)
                      if cooldown > 0:
                          sl_cooldowns[pair] = cooldown - 1
                          logger.info(f"[{pair}] BUY consenso ignorado — cooldown ({cooldown}c)")
                      else:
                          trade_usd = TRADE_AMOUNT_BRL / usd_brl
                          if engine.balance_usd < trade_usd * 1.006:
                              logger.info(f"[{pair}] BUY consenso negado — saldo insuficiente")
                          else:
                              qty = trade_usd / price
                              strats_buying = [s.name for s in all_strategies
                                               if pair_signals.get(s.name) == "BUY"]
                              if engine.buy(symbol, trade_usd, price,
                                            f"consenso:{'+'.join(strats_buying[:2])}"):
                                  pos["qty"]     = qty
                                  pos["entry"]   = price
                                  pos["peak"]    = price
                                  pos["pyramids"] = 0
                                  _record_trade("BUY", pair, qty, price, trade_usd,
                                                f"consenso·score{buy_score}·{'·'.join(strats_buying)}")
                                  logger.info(f"[{pair}] ✅ BUY CONSENSO score={buy_score}/4 "
                                              f"R${TRADE_AMOUNT_BRL:.0f} @ ${price:,.2f} "
                                              f"({'+'.join(strats_buying)})")

                  # Atualiza P&L não realizado
                  if pos["qty"] > 0:
                      pos["unrealized"] = (price - pos["entry"]) * pos["qty"]
                  else:
                      pos["unrealized"] = 0.0

                # ── Slot manual: SL/TP/Trailing ──────────────────────────────
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
                            logger.info(f"[{pair}][manual] {reason} @ ${price:,.2f}")
                    else:
                        manual_slot["unrealized"] = (price - manual_slot["entry"]) * manual_slot["qty"]

                # ── Monta positions_detail para o dashboard ───────────────────
                pos       = positions[pair]
                cooldown  = sl_cooldowns.get(pair, 0)
                buy_score = pair_signals.get("buy_score",  0)
                sel_score = pair_signals.get("sell_score", 0)

                if pos["qty"] > 0 and pos["entry"] > 0:
                    g_pct    = (price - pos["entry"]) / pos["entry"] * 100
                    sl_price = round(pos["entry"] * (1 - INITIAL_SL_PCT    / 100), 2)
                    tp_price = round(pos["entry"] * (1 + TAKE_PROFIT_PCT   / 100), 2)
                    tr_price = round(pos["peak"]  * (1 - TRAILING_STOP_PCT / 100), 2)
                else:
                    g_pct = sl_price = tp_price = tr_price = None

                trade_usd_needed = TRADE_AMOUNT_BRL / usd_brl
                bal_ok = engine.balance_usd >= trade_usd_needed * 1.006

                if pos["qty"] > 0:
                    pd_status = "em_posicao"
                    pd_note   = f"+{pos['pyramids']}▲ pyramid" if pos.get("pyramids", 0) else ""
                elif cooldown > 0:
                    pd_status = "cooldown"
                    pd_note   = f"{cooldown} ciclo{'s' if cooldown>1 else ''} restante{'s' if cooldown>1 else ''}"
                elif not bal_ok:
                    pd_status = "saldo_insuf"
                    pd_note   = f"saldo US${engine.balance_usd:.0f} (necesário US${trade_usd_needed:.0f})"
                elif buy_score >= CONSENSUS_BUY_MIN:
                    pd_status = "pronto"
                    pd_note   = f"score {buy_score}/4 — comprando"
                else:
                    pd_status = "aguardando"
                    pd_note   = f"score {buy_score}/4 (mín. {CONSENSUS_BUY_MIN})"

                state["positions_detail"][pair] = {
                    "pair":      pair,
                    "status":    pd_status,
                    "note":      pd_note,
                    "buy_score":  buy_score,
                    "sell_score": sel_score,
                    "sell_strats": sell_strats if "sell_strats" in dir() else [],
                    "qty":       round(pos["qty"], 8),
                    "entry":     round(pos["entry"], 2) if pos["entry"] else None,
                    "gain_pct":  round(g_pct, 2)    if g_pct    is not None else None,
                    "sl":        sl_price,
                    "tp":        tp_price,
                    "trailing":  tr_price,
                    "pyramids":  pos.get("pyramids", 0),
                    "cooldown":  cooldown,
                    "signals":   {s.name: pair_signals.get(s.name, "HOLD") for s in all_strategies},
                }

                _save_positions(positions)
                state["positions"] = positions

                rsi_val     = get_rsi_value(candles_1h)
                entry_price = pos["entry"] if pos["qty"] > 0 else engine.entry_prices.get(symbol)
                change_pct  = ((price - entry_price) / entry_price * 100) if entry_price else None
                state["signals"][pair] = {
                    "strategies":  pair_signals,
                    "trend":       trend,
                    "vol_guard":   vol_signal,
                    "rsi":         rsi_val,
                    "buy_score":   pair_signals.get("buy_score", 0),
                    "sell_score":  pair_signals.get("sell_score", 0),
                    "entry_price": round(entry_price, 2) if entry_price else None,
                    "change_pct":  round(change_pct,  2) if change_pct is not None else None,
                    "sl_level":    round(entry_price * (1 - INITIAL_SL_PCT  / 100), 2) if entry_price else None,
                    "tp_level":    round(entry_price * (1 + TAKE_PROFIT_PCT / 100), 2) if entry_price else None,
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
