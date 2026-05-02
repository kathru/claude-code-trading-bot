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
from strategies.ma_crossover import MACrossoverStrategy
from strategies.rsi import RSIStrategy
from strategies.scalping import ScalpingStrategy
from strategies.whale import WhaleStrategy
from logger import setup_logger, log_cycle, log_trade, log_portfolio
from notifier import notify_trade

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), "code.env"))

app = FastAPI()
HTML_FILE    = os.path.join(os.path.dirname(__file__), "templates", "index.html")
STATIC_DIR   = os.path.join(os.path.dirname(__file__), "static")

from fastapi.staticfiles import StaticFiles
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
HISTORY_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "portfolio_history.json")


def _fetch_usd_brl() -> float:
    try:
        r = requests.get("https://api.frankfurter.app/latest?from=USD&to=BRL", timeout=5)
        return float(r.json()["rates"]["BRL"])
    except Exception:
        return state.get("usd_brl", 5.70)   # fallback


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
TRADE_MAX_BRL  = 250.0       # máximo por operação em R$
TRADE_MIN_BRL  = 25.0        # mínimo por operação em R$
CONSENSUS_MIN  = 2           # mínimo de votos para executar (2 de 3 estratégias técnicas)
CYCLE_INTERVAL = 60          # segundos entre ciclos
CANDLE_GRANULARITY = "FIFTEEN_MINUTE"
STOP_LOSS_PCT  = 3.0         # vende tudo se cair 3% do preço de entrada
TAKE_PROFIT_PCT = 5.0        # vende tudo se subir 5% do preço de entrada
WHALE_INTERVAL  = 300        # executa whale a cada 5 minutos (segundos)


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

# Estratégias técnicas — votação por consenso (2/3)
tech_strategies = [
    MACrossoverStrategy(short_window=9, long_window=21),
    RSIStrategy(period=14, oversold=30, overbought=70),
    ScalpingStrategy(bb_period=20, bb_std=2.0),
]

# Whale — execução independente (order book, não candles)
whale_strategy = WhaleStrategy(whale_multiplier=5.0, top_levels=50, dominance_ratio=1.5)

# Lista unificada apenas para feed de sinais
strategies = tech_strategies + [whale_strategy]

# Controle de sinal anterior por (pair, strategy) para evitar re-execução no mesmo sinal
last_signals: dict = {}
last_whale_run: float = 0.0   # timestamp da última execução da análise whale

logger = setup_logger("dashboard")
connected_clients: List[WebSocket] = []


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
        total = engine.portfolio_value()
        pnl = total - engine.initial_balance
        state["portfolio"] = {
            "usd": round(engine.balance_usd, 2),
            "total": round(total, 2),
            "pnl": round(pnl, 2),
            "pnl_pct": round((pnl / engine.initial_balance) * 100, 2),
            "holdings":        {k: round(v, 8) for k, v in engine.holdings.items()},
            "initial_balance": round(engine.initial_balance, 2),
            "total_fees_usd":  round(engine.total_fees_usd, 4),
        }
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
        total = engine.portfolio_value()
        pnl = total - engine.initial_balance
        state["portfolio"] = {
            "usd": round(engine.balance_usd, 2),
            "total": round(total, 2),
            "pnl": round(pnl, 2),
            "pnl_pct": round((pnl / engine.initial_balance) * 100, 2),
            "holdings":        {k: round(v, 8) for k, v in engine.holdings.items()},
            "initial_balance": round(engine.initial_balance, 2),
            "total_fees_usd":  round(engine.total_fees_usd, 4),
        }
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
    global last_whale_run
    logger.info("Dashboard trading loop iniciado — granularidade: %s, ciclo: %ds", CANDLE_GRANULARITY, CYCLE_INTERVAL)
    while True:
        state["cycle"] += 1
        now_str = datetime.now().strftime("%H:%M:%S")
        state["last_update"] = now_str

        # Atualiza cotação USD/BRL a cada ciclo
        usd_brl = await asyncio.get_event_loop().run_in_executor(None, _fetch_usd_brl)
        state["usd_brl"] = round(usd_brl, 4)
        logger.debug(f"USD/BRL: {usd_brl:.4f}")

        for pair in PAIRS:
            symbol = pair.split("-")[0]
            try:
                candles = client.get_candles(pair, granularity=CANDLE_GRANULARITY, limit=100)
                ticker = client.get_ticker(pair)
                price = float(ticker.get("price", 0))
                if not price:
                    continue

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

                order_book = client.get_order_book(pair, limit=50)
                pair_signals = {}

                # ── Bloco técnico: MA Cross + RSI + Scalping (consenso 2/3) ──
                tech_votes = {"BUY": 0, "SELL": 0, "HOLD": 0}
                for strategy in tech_strategies:
                    df = strategy.candles_to_df(candles)
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
                logger.debug(f"[{pair}] tech_votes={tech_votes}")

                if tech_votes["BUY"] >= CONSENSUS_MIN:
                    # Convicção: proporção de votos BUY (2/3 = 67%, 3/3 = 100%)
                    conviction = tech_votes["BUY"] / len(tech_strategies)
                    trade_brl  = calc_trade_brl(conviction)
                    trade_usd  = trade_brl / state["usd_brl"]
                    qty = trade_usd / price
                    logger.info(f"[{pair}] TÉCNICO BUY {tech_votes['BUY']}/3 → convicção {conviction:.0%} → R${trade_brl}")
                    if engine.buy(symbol, trade_usd, price, "técnico"):
                        _record_trade("BUY", pair, qty, price, trade_usd, "técnico")
                    else:
                        logger.warning(f"[{pair}] BUY técnico negado (saldo: ${engine.balance_usd:.2f})")

                elif tech_votes["SELL"] >= CONSENSUS_MIN:
                    conviction = tech_votes["SELL"] / len(tech_strategies)
                    trade_brl  = calc_trade_brl(conviction)
                    held = engine.holdings.get(symbol, 0)
                    # Vende proporção da posição baseada na convicção
                    sell_qty = held * conviction if held > 0 else 0
                    logger.info(f"[{pair}] TÉCNICO SELL {tech_votes['SELL']}/3 → convicção {conviction:.0%} → {sell_qty:.6f} {symbol}")
                    if sell_qty > 0:
                        usd = sell_qty * price
                        if engine.sell(symbol, sell_qty, price, "técnico"):
                            _record_trade("SELL", pair, sell_qty, price, usd, "técnico")

                # ── Bloco whale: independente, a cada 5 minutos ──────────────
                now_ts = time.time()
                whale_due = (now_ts - last_whale_run) >= WHALE_INTERVAL
                if whale_due:
                    whale_signal = whale_strategy.analyze_book(order_book)
                else:
                    whale_signal = last_signals.get(f"{pair}:Whale", "HOLD")

                pair_signals["Whale"] = whale_signal
                key_w = f"{pair}:Whale"

                if whale_due:
                    if whale_signal != last_signals.get(key_w):
                        last_signals[key_w] = whale_signal
                        state["feed"].insert(0, {"time": now_str, "pair": pair,
                            "strategy": "Whale", "signal": whale_signal, "price": price})
                        state["feed"] = state["feed"][:100]

                    # Convicção whale: dominância de um lado sobre o outro
                    bid_vol = whale_strategy.last_whale_bid_usd
                    ask_vol = whale_strategy.last_whale_ask_usd
                    total_vol = bid_vol + ask_vol

                    if whale_signal == "BUY" and total_vol > 0:
                        # Quanto maior a dominância dos bids, mais forte o sinal
                        dominance = bid_vol / total_vol          # 0.5 a 1.0
                        conviction = (dominance - 0.5) / 0.5    # 0.0 a 1.0
                        trade_brl = calc_trade_brl(conviction)
                        trade_usd = trade_brl / state["usd_brl"]
                        qty = trade_usd / price
                        logger.info(f"[{pair}] WHALE BUY → dominância {dominance:.0%} → R${trade_brl}")
                        if engine.buy(symbol, trade_usd, price, "whale"):
                            _record_trade("BUY", pair, qty, price, trade_usd, "whale")

                    elif whale_signal == "SELL" and total_vol > 0:
                        dominance = ask_vol / total_vol
                        conviction = (dominance - 0.5) / 0.5
                        trade_brl = calc_trade_brl(conviction)
                        held = engine.holdings.get(symbol, 0)
                        sell_qty = held * conviction if held > 0 else 0
                        logger.info(f"[{pair}] WHALE SELL → dominância {dominance:.0%} → {sell_qty:.6f} {symbol}")
                        if sell_qty > 0:
                            usd = sell_qty * price
                            if engine.sell(symbol, sell_qty, price, "whale"):
                                _record_trade("SELL", pair, sell_qty, price, usd, "whale")

                rsi_val = get_rsi_value(candles)
                entry_price = engine.entry_prices.get(symbol)
                change_pct  = ((price - entry_price) / entry_price * 100) if entry_price else None
                state["signals"][pair] = {
                    "strategies":     pair_signals,
                    "tech_votes":     tech_votes,
                    "tech_decision":  tech_decision,
                    "whale_signal":   whale_signal,
                    "rsi":            rsi_val,
                    "whale_bid":      round(whale_strategy.last_whale_bid_usd),
                    "whale_ask":      round(whale_strategy.last_whale_ask_usd),
                    "whale_bids":     whale_strategy.whale_bids,
                    "whale_asks":     whale_strategy.whale_asks,
                    "entry_price":    round(entry_price, 2) if entry_price else None,
                    "change_pct":     round(change_pct, 2) if change_pct is not None else None,
                    "sl_level":       round(entry_price * (1 - STOP_LOSS_PCT/100), 2) if entry_price else None,
                    "tp_level":       round(entry_price * (1 + TAKE_PROFIT_PCT/100), 2) if entry_price else None,
                }
                log_cycle(logger, state["cycle"], pair, price, pair_signals, tech_decision)

            except Exception as e:
                state["signals"][pair] = {"error": str(e)}
                logger.error(f"[{pair}] Erro: {e}")

        # Atualiza timestamp whale após processar todos os pares
        if (time.time() - last_whale_run) >= WHALE_INTERVAL:
            last_whale_run = time.time()
            logger.info(f"Whale: análise concluída — próxima em {WHALE_INTERVAL//60}min")

        total = engine.portfolio_value()
        pnl = total - engine.initial_balance
        log_portfolio(logger, engine.balance_usd, total, pnl,
                      (pnl / engine.initial_balance) * 100, engine.holdings)
        state["portfolio"] = {
            "usd": round(engine.balance_usd, 2),
            "total": round(total, 2),
            "pnl": round(pnl, 2),
            "pnl_pct": round((pnl / engine.initial_balance) * 100, 2),
            "holdings":        {k: round(v, 8) for k, v in engine.holdings.items()},
            "initial_balance": round(engine.initial_balance, 2),
            "total_fees_usd":  round(engine.total_fees_usd, 4),
        }
        state["history"].append({
            "time": now_str,
            "ts": int(time.time()),
            "total": round(total, 2),
        })
        state["history"] = state["history"][-90000:]
        _save_history(state["history"])

        await broadcast(state)
        await asyncio.sleep(CYCLE_INTERVAL)


@app.on_event("startup")
async def startup():
    asyncio.create_task(trading_loop())
