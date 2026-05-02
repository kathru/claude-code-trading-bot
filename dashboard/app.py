import os
import sys
import time
import json
import asyncio
from datetime import datetime
from typing import List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, FileResponse
from dotenv import load_dotenv

from exchange.coinbase import CoinbaseClient
from paper_trading.engine import PaperTradingEngine
from strategies.ma_crossover import MACrossoverStrategy
from strategies.rsi import RSIStrategy
from strategies.scalping import ScalpingStrategy
from strategies.whale import WhaleStrategy
from logger import setup_logger, log_cycle, log_trade, log_portfolio
from notifier import notify_trade

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), "code.env"))

app = FastAPI()
HTML_FILE = os.path.join(os.path.dirname(__file__), "templates", "index.html")
HISTORY_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "portfolio_history.json")


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
TRADE_USD = 100.0            # valor único por operação
CONSENSUS_MIN = 2            # mínimo de votos para executar (2 de 4 estratégias)
CYCLE_INTERVAL = 30          # segundos entre ciclos
CANDLE_GRANULARITY = "FIFTEEN_MINUTE"
STOP_LOSS_PCT  = 3.0         # vende tudo se cair 3% do preço de entrada
TAKE_PROFIT_PCT = 5.0        # vende tudo se subir 5% do preço de entrada

client = CoinbaseClient(os.getenv("API_KEY"), os.getenv("SECRET_KEY"))
engine = PaperTradingEngine(initial_balance_usd=10000.0)
whale_strategy = WhaleStrategy(whale_multiplier=5.0, top_levels=50, dominance_ratio=1.5)
strategies = [
    MACrossoverStrategy(short_window=9, long_window=21),
    RSIStrategy(period=14, oversold=30, overbought=70),
    ScalpingStrategy(bb_period=20, bb_std=2.0),
    whale_strategy,
]

# Controle de sinal anterior por (pair, strategy) para evitar re-execução no mesmo sinal
last_signals: dict = {}

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
            "strategy": t.get("strategy", ""),
        })
    return result


state = {
    "prices": {},
    "signals": {},
    "portfolio": {"usd": engine.balance_usd, "total": engine.balance_usd, "pnl": 0.0, "pnl_pct": 0.0},
    "trades": _load_trades_from_engine(),
    "feed": [],
    "history": _load_history(),
    "cycle": 0,
    "status": "running",
    "last_update": "",
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
async def manual_buy(pair: str, usd: float = 100.0):
    symbol = pair.split("-")[0]
    ticker = client.get_ticker(pair)
    price = float(ticker.get("price", 0))
    if not price:
        return {"ok": False, "error": "Preço indisponível"}
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
            "holdings": {k: round(v, 8) for k, v in engine.holdings.items()},
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
            "holdings": {k: round(v, 8) for k, v in engine.holdings.items()},
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
    log_trade(logger, side, pair, qty, price, usd, strategy)
    notify_trade(side, pair, qty, price, usd)
    state["trades"].insert(0, {
        "time": datetime.now().strftime("%H:%M:%S"),
        "side": side, "pair": pair,
        "price": price, "usd": usd,
        "strategy": strategy,
    })
    state["trades"] = state["trades"][:50]


async def trading_loop():
    logger.info("Dashboard trading loop iniciado — granularidade: %s, ciclo: %ds", CANDLE_GRANULARITY, CYCLE_INTERVAL)
    while True:
        state["cycle"] += 1
        now_str = datetime.now().strftime("%H:%M:%S")
        state["last_update"] = now_str

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
                votes = {"BUY": 0, "SELL": 0, "HOLD": 0}

                for strategy in strategies:
                    if isinstance(strategy, WhaleStrategy):
                        signal = strategy.analyze_book(order_book)
                    else:
                        df = strategy.candles_to_df(candles)
                        signal = strategy.analyze(df)
                    pair_signals[strategy.name] = signal
                    votes[signal] += 1

                    # Feed ao vivo: registra sempre que o sinal mudar
                    key = f"{pair}:{strategy.name}"
                    prev = last_signals.get(key)
                    if signal != prev:
                        last_signals[key] = signal
                        state["feed"].insert(0, {
                            "time": now_str, "pair": pair,
                            "strategy": strategy.name,
                            "signal": signal, "price": price,
                        })
                        state["feed"] = state["feed"][:100]

                rsi_val = get_rsi_value(candles)
                decision = max(votes, key=votes.get)
                entry_price = engine.entry_prices.get(symbol)
                change_pct  = ((price - entry_price) / entry_price * 100) if entry_price else None
                state["signals"][pair] = {
                    "strategies":  pair_signals,
                    "votes":       votes,
                    "decision":    decision,
                    "rsi":         rsi_val,
                    "whale_bid":   round(whale_strategy.last_whale_bid_usd),
                    "whale_ask":   round(whale_strategy.last_whale_ask_usd),
                    "whale_bids":  whale_strategy.whale_bids,
                    "whale_asks":  whale_strategy.whale_asks,
                    "entry_price": round(entry_price, 2) if entry_price else None,
                    "change_pct":  round(change_pct, 2) if change_pct is not None else None,
                    "sl_level":    round(entry_price * (1 - STOP_LOSS_PCT/100), 2) if entry_price else None,
                    "tp_level":    round(entry_price * (1 + TAKE_PROFIT_PCT/100), 2) if entry_price else None,
                }
                log_cycle(logger, state["cycle"], pair, price, pair_signals, decision)

                # Execução por consenso: mínimo CONSENSUS_MIN votos
                logger.debug(f"[{pair}] votes={votes} decision={decision}")
                if votes["BUY"] >= CONSENSUS_MIN:
                    qty = TRADE_USD / price
                    logger.info(f"[{pair}] CONSENSO BUY ({votes['BUY']}/4) — tentando comprar ${TRADE_USD}")
                    if engine.buy(symbol, TRADE_USD, price, "consensus"):
                        _record_trade("BUY", pair, qty, price, TRADE_USD, "consensus")
                    else:
                        logger.warning(f"[{pair}] BUY negado pelo engine (saldo: ${engine.balance_usd:.2f})")

                elif votes["SELL"] >= CONSENSUS_MIN:
                    held = engine.holdings.get(symbol, 0)
                    logger.info(f"[{pair}] CONSENSO SELL ({votes['SELL']}/4) — posição: {held}")
                    if held > 0:
                        usd = held * price
                        if engine.sell(symbol, held, price, "consensus"):
                            _record_trade("SELL", pair, held, price, usd, "consensus")
                        else:
                            logger.warning(f"[{pair}] SELL negado pelo engine")

            except Exception as e:
                state["signals"][pair] = {"error": str(e)}
                logger.error(f"[{pair}] Erro: {e}")

        total = engine.portfolio_value()
        pnl = total - engine.initial_balance
        log_portfolio(logger, engine.balance_usd, total, pnl,
                      (pnl / engine.initial_balance) * 100, engine.holdings)
        state["portfolio"] = {
            "usd": round(engine.balance_usd, 2),
            "total": round(total, 2),
            "pnl": round(pnl, 2),
            "pnl_pct": round((pnl / engine.initial_balance) * 100, 2),
            "holdings": {k: round(v, 8) for k, v in engine.holdings.items()},
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
