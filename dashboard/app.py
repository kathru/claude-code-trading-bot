import os
import sys
import time
import json
import asyncio
import requests
import pandas as pd
from datetime import datetime
from typing import List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, FileResponse
from dotenv import load_dotenv

from exchange.coinbase import CoinbaseClient
from paper_trading.engine import PaperTradingEngine, TAKER_FEE
# ── Suite agressiva v2 (alvo +65%): trend-following + momentum ─────
from strategies.donchian_breakout      import DonchianBreakout
from strategies.ema_pullback           import EMAPullback
from strategies.macd_momentum          import MACDMomentum
from strategies.stoch_bounce           import StochBounce
from strategies.rsi_divergence_detector import RSIDivergenceDetector
from strategies.volatility_guard       import VolatilityGuard
from strategies.trend_filter           import TrendFilter
from strategies.market_regime          import (
    detect_regime, calc_atr, atr_stop_loss,
    obv_rising, mfi_bullish, mtf_trend_bullish
)
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
    # Tenta múltiplas APIs como fallback
    apis = [
        ("https://api.frankfurter.dev/v1/latest?from=USD&to=BRL", lambda d: float(d["rates"]["BRL"])),
        ("https://open.er-api.com/v6/latest/USD", lambda d: float(d["rates"]["BRL"])),
    ]
    for url, parser in apis:
        try:
            r = requests.get(url, timeout=5)
            rate = parser(r.json())
            if 3.0 < rate < 10.0:   # sanity check
                _usd_brl_cache["rate"] = rate
                _usd_brl_cache["ts"]   = now
                return rate
        except Exception:
            continue
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
# Refresh a cada 3 minutos para acompanhar cycle de 180s e capturar breakouts
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
    """TP dinâmico entre TAKE_PROFIT_MIN e MAX inversamente proporcional ao Fear & Greed.
    Medo   → alvo maior (deixa winners correrem mais)
    Ganância → alvo menor (realiza antes da euforia acabar)
    """
    rng = TAKE_PROFIT_MAX - TAKE_PROFIT_MIN
    if   fg_value <= 25: return TAKE_PROFIT_MAX
    elif fg_value <= 40: return round(TAKE_PROFIT_MIN + rng * 0.67, 1)
    elif fg_value <= 60: return round(TAKE_PROFIT_MIN + rng * 0.50, 1)
    elif fg_value <= 74: return round(TAKE_PROFIT_MIN + rng * 0.25, 1)
    else:                return TAKE_PROFIT_MIN


def _dynamic_sl(fg_value: int) -> float:
    """SL dinâmico entre SL_MIN e SL_MAX — proporcional ao Fear & Greed (inverso ao TP).
    Medo extremo → SL mais largo (mercado volátil, evita stop prematuro)
    Ganância extrema → SL mais apertado (sai rápido de posições ruins em euforia)
    """
    rng = SL_MAX - SL_MIN
    if   fg_value <= 25: return SL_MAX                            # Medo extremo: -7%
    elif fg_value <= 40: return round(SL_MIN + rng * 0.67, 1)    # -5.7%
    elif fg_value <= 60: return round(SL_MIN + rng * 0.50, 1)    # Neutro: -5%
    elif fg_value <= 74: return round(SL_MIN + rng * 0.25, 1)    # -4%
    else:                return SL_MIN                            # Ganância extrema: -3%


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


PAIRS = ["BTC-USD", "ETH-USD", "SOL-USD", "AVAX-USD", "LINK-USD", "DOGE-USD"]  # 6 pares para melhor diversificação

# ── Portfolio em Real é FIXO em R$ 4.000 ────────────────────────
TOTAL_BRL_INITIAL = 5000.0  # Portfolio inicial em BRL — FIXO, nunca muda
# Portfolio em USD varia com cotação: USD_atual = TOTAL_BRL_INITIAL / usd_brl_atual

# ── Ciclo e candles ─────────────────────────────────────────────
CYCLE_INTERVAL    = 300      # ciclo de 300s (5 minutos)
CANDLE_30M        = "THIRTY_MINUTE"  # Donchian, Stoch
CANDLE_1H         = "ONE_HOUR"       # EMA Pullback, MACD
CANDLE_6H         = "SIX_HOUR"
CANDLE_1D         = "ONE_DAY"        # Trend, VolGuard

# ── Execução por estratégia (independente, sem consenso) ──────────
TRADE_PCT          = 0.10   # 10% do portfolio por trade — reduzido para diminuir taxas e risco


def _calculate_dynamic_position_size(pair: str, candles: list, base_pct: float = None) -> float:
    """Calcula tamanho de posição dinamicamente baseado em volatilidade.

    Alta volatilidade → posição menor (2%)
    Baixa volatilidade → posição maior (até 10%)
    """
    if base_pct is None:
        base_pct = TRADE_PCT

    if len(candles) < 20:
        return base_pct  # Fallback ao base

    df = pd.DataFrame(candles, columns=["start", "low", "high", "open", "close", "volume"])
    df["close"] = df["close"].astype(float)
    df["high"] = df["high"].astype(float)
    df["low"] = df["low"].astype(float)

    # Calcular ATR (Average True Range)
    df["tr"] = df["high"] - df["low"]
    atr_current = df["tr"].iloc[-1]
    atr_avg = df["tr"].tail(14).mean()

    # Ratio de volatilidade (inverso: mais vol = posição menor)
    if atr_current > 0:
        vol_ratio = atr_avg / atr_current
    else:
        vol_ratio = 1.0

    # Aplicar ratio com limites (2% mín, TRADE_PCT máx)
    size = base_pct * vol_ratio
    size = max(0.02, min(TRADE_PCT, size))

    return size


# ── Gestão de risco ──────────────────────────────────────────────
SL_MIN                = 3.0  # SL mínimo: -3% (ganância extrema — sai rápido)
SL_MAX                = 7.0  # SL máximo: -7% (medo extremo — mais espaço para respirar)
TAKE_PROFIT_MIN       = 4.0  # TP mínimo: +4%
TAKE_PROFIT_MAX       = 12.0 # TP máximo: +12%
TRAILING_STOP_PCT     = 2.5  # trailing: -2.5% do pico
TRAILING_ACTIVATE_PCT = 2.0  # trailing ativa após +2%
BREAKEVEN_ACTIVATE_PCT = 1.5 # após +1.5%, SL sobe para entrada (risco zero)
SL_COOLDOWN_CYCLES    = 3    # após SL, aguarda 3 ciclos (9min) antes de re-entrar

# ── Pyramid (scale-in em posição lucrativa) ──────────────────────
PYRAMID_MAX          = 3     # máx. 3 adições (175% da entrada total)
PYRAMID_MIN_GAIN_PCT = 1.0   # só adiciona se ≥ +1.0% no lucro — confirma tendência antes de escalar
PYRAMID_SIZE_PCT     = 0.25  # cada pyramid = 25% do trade inicial

# ── Fear & Greed ─────────────────────────────────────────────────
FG_FEAR_MAX    = 25   # Medo Extremo: entrada direta, sem restrições
FG_GREED_MIN   = 70   # Ganância Moderada: permite entradas agressivas a partir de 70 (entra em uptrends cedo)
FG_TTL         = 3600 # cache de 1 hora (índice atualiza 1×/dia)

client = CoinbaseClient(os.getenv("API_KEY"), os.getenv("SECRET_KEY"))
engine = PaperTradingEngine(initial_balance_usd=10000.0)

# ── 5 estratégias — agressivas com qualidade ────────────────────────────
all_strategies = [
    DonchianBreakout(
        period=20,
        rsi_min=45.0,       # RSI 45: menos restritivo, mais oportunidades
        vol_mult=1.0,       # sem exigência de spike de volume
        adx_min=20.0,       # ADX 20: aceita tendências moderadas (era 25 — muito restritivo)
        obv_lookback=5,     # OBV lookback curto: mais responsivo (era 10)
    ),
    EMAPullback(
        fast=9, mid=21, slow=50,
        touch_tolerance_pct=0.5,    # 0.5%: mais tolerante ao toque na EMA (era 0.3)
        slope_bars=5,
        vol_pullback_mult=1.2,      # relaxado: pullback pode ter volume até 1.2× (era 0.8)
        vol_breakout_mult=1.0,      # relaxado: retomada acima da média (era 1.2)
    ),
    MACDMomentum(
        fast=12, slow=26, signal=9,
        ema_filter=12,      # EMA12: resposta rápida
        rsi_max=75.0,       # RSI 75: mais espaço antes de considerar sobrecomprado (era 70)
    ),
    StochBounce(
        k_period=9,         # k=9: mais rápido e responsivo
        d_period=3,
        oversold=25.0,      # 25: mais oportunidades (era 20 — muito restritivo)
        overbought=75.0,
        ma_filter=50,
        bb_bandwidth_max=0.22,  # 22%: crypto é naturalmente volátil (era 15% — bloqueava demais)
    ),
    RSIDivergenceDetector(period=14, lookback_periods=5),
]

# Mapa de candles por estratégia
STRAT_CANDLES = {
    "Donchian Breakout":     CANDLE_30M,
    "EMA Pullback":          CANDLE_1H,
    "MACD Momentum":         CANDLE_1H,
    "Stoch Bounce":          CANDLE_30M,
    "RSI Divergence Detect": CANDLE_30M,
}

# Guard de risco global — só dispara em crashes REAIS (>25% vol = mercado caindo)
vol_guard    = VolatilityGuard(threshold_pct=25.0, consecutive_days=3)  # 25% — permite volatilidade normal de crypto
trend_filter = TrendFilter(period=50)   # EMA50 1H — alinhado com EMA Pullback e MACD Momentum

# ── Cooldown anti-whipsaw após SL (por slot) ─────────────────────
sl_cooldowns: dict = {}   # {f"{strat}:{pair}": cycles_remaining}

# ── Cooldown entre BUYs do mesmo par (1h mínimo entre compras) ───
last_buy_time: dict = {}  # {f"{strat}:{pair}": timestamp}
BUY_COOLDOWN_SECONDS = 3600  # 1 hora entre BUYs no mesmo par/estratégia

# ── Limite de posições simultâneas abertas ───────────────────────
MAX_OPEN_SLOTS = 4            # máximo de slots abertos ao mesmo tempo

# ── Limite de trades por dia (circuit breaker) ──────────────────
_daily_trade_count: dict = {}  # {"YYYY-MM-DD": count}
MAX_DAILY_TRADES = 15         # máximo de trades por dia (evita overtrading)

# ── Slots independentes: 4 estratégias × 3 pares + 3 manuais ────
def _empty_slot():
    return {"qty": 0.0, "entry": 0.0, "peak": 0.0,
            "realized": 0.0, "unrealized": 0.0, "pyramids": 0, "be_sl": 0.0,
            "entry_usd": 0.0}  # tamanho da entrada original — pyramid usa esse valor

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
    pnl = {s.name: {"realized": 0.0, "trades": 0, "buys": 0, "sells": 0}
           for s in all_strategies}
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
    """Registra P&L realizado e contabiliza o sell na estratégia."""
    if strat_name in strategy_pnl:
        strategy_pnl[strat_name]["realized"] += pnl_usd
        strategy_pnl[strat_name]["sells"]    = strategy_pnl[strat_name].get("sells", 0) + 1
        strategy_pnl[strat_name]["trades"]   = (strategy_pnl[strat_name].get("buys", 0)
                                                + strategy_pnl[strat_name]["sells"])
    _save_strategy_pnl(strategy_pnl)
    state["strategy_pnl"] = strategy_pnl

def _count_buy(strat_name: str):
    """Contabiliza um BUY na estratégia."""
    if strat_name in strategy_pnl:
        strategy_pnl[strat_name]["buys"]   = strategy_pnl[strat_name].get("buys", 0) + 1
        strategy_pnl[strat_name]["trades"] = (strategy_pnl[strat_name]["buys"]
                                              + strategy_pnl[strat_name].get("sells", 0))
    _save_strategy_pnl(strategy_pnl)
    state["strategy_pnl"] = strategy_pnl

last_signals: dict = {}   # {f"{pair}:{strat}": signal}

logger = setup_logger("dashboard")
connected_clients: List[WebSocket] = []


def _update_portfolio_state():
    """Calcula P&L considerando que portfolio em BRL é FIXO (R$ 4.000).

    Lógica:
    - Portfolio em BRL é sempre R$ 4.000 (fixo)
    - Portfolio em USD = R$ 4.000 / USD/BRL (varia com cotação)
    - P&L_BRL = (saldo_atual_em_USD × USD/BRL) - R$ 4.000
    - P&L_USD = saldo_atual_em_USD - (R$ 4.000 / USD/BRL_inicial)

    ⚠️ IMPORTANTE: Mudanças em USD/BRL NÃO devem contar como P&L
    """
    total_usd = engine.portfolio_value()
    usd_brl_current = state.get("usd_brl", 5.70)  # cotação atual

    # Calcular P&L apenas em TRADES, não em cotação
    # P&L em USD = resultado atual - resultado ao final do último ciclo completo
    # Mas para simplicidade: P&L = total - initial (que já é em USD no reset)
    pnl_usd = total_usd - engine.initial_balance

    # Converter tudo para BRL usando cotação ATUAL
    total_brl = total_usd * usd_brl_current
    pnl_brl = total_brl - TOTAL_BRL_INITIAL  # P&L real em BRL
    pnl_pct = (pnl_brl / TOTAL_BRL_INITIAL) * 100 if TOTAL_BRL_INITIAL > 0 else 0

    state["portfolio"] = {
        "usd":               round(engine.balance_usd, 2),
        "total_usd":         round(total_usd, 2),
        "total_brl":         round(total_brl, 2),
        "pnl_usd":           round(pnl_usd, 2),
        "pnl_brl":           round(pnl_brl, 2),
        "pnl_pct":           round(pnl_pct, 2),
        "initial_balance_usd": round(engine.initial_balance, 2),
        "initial_balance_brl": round(TOTAL_BRL_INITIAL, 2),
        "holdings":          {k: round(v, 8) for k, v in engine.holdings.items()},
        "total_fees_usd":    round(engine.total_fees_usd, 4),
    }
    return total_usd, pnl_usd


def _calculate_kpis() -> dict:
    """Calcula métricas de performance usando strategy_pnl e preços de entrada/saída."""
    all_trades  = engine.trades
    sell_trades = [t for t in all_trades if t.get("side") == "SELL"]

    if not sell_trades:
        return {
            "total_trades": len(all_trades),
            "sell_trades":  0,
            "win_rate":     0.0,
            "win_count":    0,
            "loss_count":   0,
            "avg_win":      0.0,
            "avg_loss":     0.0,
            "profit_factor": 0.0,
            "expected_value": 0.0,
        }

    # Reconstrói custo médio de entrada por símbolo a partir de engine.trades
    # engine.trades usa campo "symbol" (ex: "LINK") e tem "qty"
    def _avg_entry_at_sell(trades_history, sell_idx):
        """Calcula preço médio de entrada no momento do SELL usando trades anteriores."""
        sell   = trades_history[sell_idx]
        # engine.trades usa "symbol" (ex:"LINK"), não "pair" (ex:"LINK-USD")
        symbol = sell.get("symbol") or sell.get("pair", "").replace("-USD", "")
        running_qty, running_cost = 0.0, 0.0
        for t in trades_history[:sell_idx]:
            t_sym = t.get("symbol") or t.get("pair", "").replace("-USD", "")
            if t_sym != symbol:
                continue
            if t.get("side") == "BUY":
                q = t.get("qty", 0)
                running_qty  += q
                running_cost += q * t.get("price", 0)
            elif t.get("side") == "SELL":
                q = min(t.get("qty", 0), running_qty)
                if running_qty > 1e-10:
                    running_cost *= (running_qty - q) / running_qty
                running_qty = max(0, running_qty - q)
        return running_cost / running_qty if running_qty > 1e-10 else 0.0

    # Calcula P&L por trade de SELL (engine.trades tem "qty")
    all_trades_list = list(all_trades)
    trade_pnls = []
    for idx, t in enumerate(all_trades_list):
        if t.get("side") != "SELL":
            continue
        sell_usd = t.get("usd", 0)
        qty      = t.get("qty", 0)
        entry    = _avg_entry_at_sell(all_trades_list, idx)
        if entry > 0 and qty > 0:
            # buy_fee: custo da compra (0.6% sobre o valor de compra)
            # sell_usd já é líquido da taxa de venda
            buy_fee  = qty * entry * TAKER_FEE
            cost_usd = qty * entry + buy_fee
            pnl      = sell_usd - cost_usd
        else:
            pnl = 0.0
        trade_pnls.append(pnl)

    win_pnls  = [p for p in trade_pnls if p > 0]
    loss_pnls = [p for p in trade_pnls if p <= 0]
    wins      = len(win_pnls)
    losses    = len(loss_pnls)

    avg_win  = sum(win_pnls)  / wins   if wins   > 0 else 0.0
    avg_loss = sum(loss_pnls) / losses if losses > 0 else 0.0

    sum_wins      = sum(win_pnls)       if win_pnls  else 0.0
    sum_losses    = abs(sum(loss_pnls)) if loss_pnls else 0.0
    profit_factor = sum_wins / sum_losses if sum_losses > 0 else 0.0

    n = len(sell_trades)
    return {
        "total_trades":  len(all_trades),
        "sell_trades":   n,
        "win_rate":      (wins / n) if n > 0 else 0.0,   # decimal 0-1 (frontend multiplica por 100)
        "win_count":     wins,
        "loss_count":    losses,
        "avg_win":       avg_win,
        "avg_loss":      avg_loss,
        "profit_factor": profit_factor,
        "expected_value": (avg_win * wins + avg_loss * losses) / n if n > 0 else 0.0,
    }


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
    # FIX: campos BRL completos no state inicial — evita saldo US$ 10.000 na conexão WebSocket
    "portfolio": {
        "usd":                 round(engine.balance_usd, 2),
        "total_usd":           round(engine.portfolio_value(), 2),
        "total_brl":           round(engine.portfolio_value() * 5.70, 2),
        "pnl_usd":             0.0,
        "pnl_brl":             0.0,
        "pnl_pct":             0.0,
        "initial_balance_usd": round(engine.initial_balance, 2),
        "initial_balance_brl": round(TOTAL_BRL_INITIAL, 2),
        "holdings":            {k: round(v, 8) for k, v in engine.holdings.items()},
        "total_fees_usd":      round(engine.total_fees_usd, 4),
    },
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
    "kpis":             _calculate_kpis(),  # Métricas de performance
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

    # ── Portfolio em BRL é FIXO (R$ 4.000) ────────────────────────────
    ALLOC_BRL = TOTAL_BRL_INITIAL / 4      # R$1.000 por cripto (1000 + 1000 + 1000 + 1000)
    alloc_usd = ALLOC_BRL / usd_brl        # valor em USD na cotação atual
    total_usd = TOTAL_BRL_INITIAL / usd_brl  # portfolio total em USD na cotação atual

    # ── Reinicia engine DIRETAMENTE — sem compra, sem taxas ──────
    # Portfólio: 100% em caixa — sem posições pré-carregadas
    # Estratégias compram naturalmente via sinais → histórico 100% real
    # P&L = portfolio_value() - initial_balance = total_usd - total_usd = 0,00 ✅
    engine.initial_balance = total_usd
    engine.balance_usd     = total_usd   # tudo em caixa
    engine.holdings        = {}
    engine.entry_prices    = {}
    engine.trades          = []
    engine.total_fees_usd  = 0.0
    engine.prices          = {p.split("-")[0]: prices[p] for p in PAIRS}
    engine._save_state()

    # ── Todos os slots zerados — sem posições artificiais ────────
    for s in all_strategies:
        for pair in PAIRS:
            strategy_slots[f"{s.name}:{pair}"] = _empty_slot()
    for pair in PAIRS:
        strategy_slots[f"manual:{pair}"] = _empty_slot()
    _save_slots(strategy_slots)
    state["slots"] = strategy_slots

    # ── Reinicia P&L por estratégia ──────────────────────────────
    for name in strategy_pnl:
        strategy_pnl[name] = {"realized": 0.0, "trades": 0, "buys": 0, "sells": 0}
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
        "total_brl": round(TOTAL_BRL_INITIAL, 2),
        "usd_brl":   round(usd_brl, 4),
        "total_usd": round(total_usd, 2),
        "cash_usd":  round(total_usd, 2),
        "cash_brl":  round(TOTAL_BRL_INITIAL, 2),
        "note":      "Portfolio em R$ 4.000 (fixo) — variação USD/BRL não afeta P&L",
        "slots":     "todos zerados — estratégias operam por sinal",
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
    """Broadcast estado com timeout para evitar travamentos de clientes lentos"""
    dead = []
    for ws in connected_clients:
        try:
            await asyncio.wait_for(ws.send_json(data), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("WebSocket send timeout - removendo cliente")
            dead.append(ws)
        except Exception as e:
            logger.debug(f"WebSocket send error: {e}")
            dead.append(ws)
    for ws in dead:
        try:
            connected_clients.remove(ws)
        except ValueError:
            pass


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
    loop = asyncio.get_event_loop()
    while True:
        state["cycle"] = _current_cycle()
        now_str = datetime.now().strftime("%H:%M:%S")
        state["last_update"]    = now_str
        state["cycle_start_ts"] = int(time.time())

        try:
            usd_brl = await asyncio.wait_for(
                loop.run_in_executor(None, _fetch_usd_brl),
                timeout=10.0
            )
        except asyncio.TimeoutError:
            logger.warning("USD/BRL fetch timeout - usando cache")
            usd_brl = _usd_brl_cache["rate"]
        state["usd_brl"] = round(usd_brl, 4)
        state["trade_pct"] = TRADE_PCT  # máximo — o real por par é dinâmico (2-10%)

        # Calcula portfolio_total
        portfolio_total = engine.portfolio_value()
        state["trade_amount_brl"] = round(portfolio_total * TRADE_PCT * usd_brl, 2)

        # Fear & Greed (cache 1h — non-blocking via executor)
        try:
            fg = await asyncio.wait_for(
                loop.run_in_executor(None, _fetch_fear_greed),
                timeout=10.0
            )
        except asyncio.TimeoutError:
            logger.warning("Fear&Greed fetch timeout - usando cache")
            fg = _fg_cache
        state["fear_greed"] = {"value": fg["value"], "label": fg["label"]}
        fg_value       = fg["value"]
        current_tp_pct = _dynamic_tp(fg_value)
        current_sl_pct = _dynamic_sl(fg_value)
        state["tp_objective"] = {"min": TAKE_PROFIT_MIN, "max": TAKE_PROFIT_MAX, "current": current_tp_pct}
        state["sl_objective"] = {"min": SL_MIN,          "max": SL_MAX,          "current": current_sl_pct}

        _dynamic_pcts = []   # acumula dynamic_pct de cada par para média no final

        for pair in PAIRS:
            symbol = pair.split("-")[0]
            try:
                # Fetch ticker com timeout via executor (evita bloquear event loop)
                ticker = await asyncio.wait_for(
                    loop.run_in_executor(None, client.get_ticker, pair),
                    timeout=8.0
                )
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

                # Fetch candles com timeout (evita bloquear se API está lenta)
                try:
                    candles_1h = await asyncio.wait_for(
                        loop.run_in_executor(None, _get_candles, pair, CANDLE_1H, 250),
                        timeout=8.0
                    )
                except asyncio.TimeoutError:
                    logger.warning(f"[{pair}] Candles 1H timeout - usando cache")
                    candles_1h = _candle_cache.get(f"{pair}:{CANDLE_1H}", {}).get("data", [])

                try:
                    candles_6h = await asyncio.wait_for(
                        loop.run_in_executor(None, _get_candles, pair, CANDLE_6H, 100),
                        timeout=8.0
                    )
                except asyncio.TimeoutError:
                    logger.warning(f"[{pair}] Candles 6H timeout - usando cache")
                    candles_6h = _candle_cache.get(f"{pair}:{CANDLE_6H}", {}).get("data", [])

                try:
                    candles_1d = await asyncio.wait_for(
                        loop.run_in_executor(None, _get_candles, pair, CANDLE_1D, 250),
                        timeout=8.0
                    )
                except asyncio.TimeoutError:
                    logger.warning(f"[{pair}] Candles 1D timeout - usando cache")
                    candles_1d = _candle_cache.get(f"{pair}:{CANDLE_1D}", {}).get("data", [])

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
                    # vol_guard: reduz 5%/ciclo em cada slot aberto deste par
                    max_usd = portfolio_total * TRADE_PCT
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
                                # ← slot só atualiza se venda foi executada
                                rem = vslot["qty"] - vqty
                                if rem < 1e-8:
                                    vslot.update({"qty": 0.0, "entry": 0.0, "peak": 0.0, "pyramids": 0, "be_sl": 0.0})
                                else:
                                    vslot["qty"] = rem
                    _save_slots(strategy_slots)
                    logger.info(f"[{pair}] VOLATILIDADE EXTREMA — reduzindo posições {TRADE_PCT*100:.0f}%/ciclo")
                    # ← Não executa estratégias neste ciclo para evitar re-abertura imediata

                else:
                  # ── PASSO 1: coleta TODOS os sinais antes de agir ─────────
                  # (score completo no feed — sem notas parciais)
                  try:
                      candles_30m = await asyncio.wait_for(
                          loop.run_in_executor(None, _get_candles, pair, CANDLE_30M, 250),
                          timeout=8.0
                      )
                  except asyncio.TimeoutError:
                      logger.warning(f"[{pair}] Candles 30M timeout - usando cache")
                      candles_30m = _candle_cache.get(f"{pair}:{CANDLE_30M}", {}).get("data", [])
                  candle_map  = {
                      "Donchian Breakout":     candles_30m,
                      "EMA Pullback":          candles_1h,
                      "MACD Momentum":         candles_1h,
                      "Stoch Bounce":          candles_30m,
                      "RSI Divergence Detect": candles_30m,
                  }
                  signals_this_cycle = {}

                  for strat in all_strategies:
                      raw    = candle_map.get(strat.name, candles_1h)
                      df     = strat.candles_to_df(raw)
                      signal = strat.analyze(df)
                      pair_signals[strat.name]        = signal
                      signals_this_cycle[strat.name]  = signal

                  # Feed: registra mudanças de sinal com valor e percentual
                  for strat in all_strategies:
                      signal  = signals_this_cycle[strat.name]
                      sig_key = f"{pair}:{strat.name}"
                      if signal != last_signals.get(sig_key):
                          last_signals[sig_key] = signal
                          trade_usd = portfolio_total * TRADE_PCT
                          state["feed"].insert(0, {
                              "time":     now_str,
                              "cycle":    state["cycle"],
                              "pair":     pair,
                              "strategy": strat.name,
                              "signal":   signal,
                              "price":    price,
                              "executed": False,
                              "note":     f"R${trade_usd*usd_brl:.0f} ({TRADE_PCT*100:.0f}% PL)",
                          })
                          state["feed"] = state["feed"][:100]

                  # ── PASSO 2: execução independente por estratégia ────────
                  extreme_fear  = fg_value <= FG_FEAR_MAX
                  extreme_greed = fg_value >= FG_GREED_MIN

                  # Tamanho dinâmico por par — usa candles 1h (mais estável)
                  dynamic_pct = _calculate_dynamic_position_size(pair, candles_1h)
                  _dynamic_pcts.append(dynamic_pct)

                  # ── Filtros de regime e confirmação multi-timeframe ──────
                  df_30m_regime  = all_strategies[0].candles_to_df(candles_30m) if candles_30m else pd.DataFrame()
                  df_1h_regime   = all_strategies[0].candles_to_df(candles_1h)  if candles_1h  else pd.DataFrame()
                  df_6h_regime   = all_strategies[0].candles_to_df(candles_6h)  if candles_6h  else pd.DataFrame()

                  # ADX: detecta regime no 1H (base para tendência)
                  market_regime  = detect_regime(df_1h_regime) if len(df_1h_regime) > 30 else "neutral"

                  # MTF: preço acima da EMA50 no 6H (confirma tendência maior)
                  mtf_bullish    = mtf_trend_bullish(df_6h_regime) if len(df_6h_regime) > 50 else True

                  # ATR: volatilidade atual do par no 1H (para SL dinâmico)
                  current_atr    = calc_atr(df_1h_regime) if len(df_1h_regime) > 14 else 0.0

                  # Circuit breaker diário
                  today_str      = now_str[:10]
                  daily_trades   = _daily_trade_count.get(today_str, 0)
                  circuit_open   = daily_trades >= MAX_DAILY_TRADES

                  # Posições simultâneas abertas (global)
                  open_slots_count = sum(1 for s in strategy_slots.values() if s.get("qty", 0) > 0)

                  logger.debug(f"[{pair}] regime={market_regime} mtf={mtf_bullish} "
                               f"atr={current_atr:.4f} open_slots={open_slots_count} daily={daily_trades}")

                  for strat in all_strategies:
                      key    = f"{strat.name}:{pair}"
                      slot   = strategy_slots.setdefault(key, _empty_slot())
                      signal = signals_this_cycle.get(strat.name, "HOLD")

                      if slot["qty"] > 0:
                          # ── Limpa posições de "pó" (valor < $0.50) ──────────────
                          slot_value_usd = slot["qty"] * price
                          if slot_value_usd < 0.50:
                              logger.info(f"[{pair}][{strat.name}] Zerando posição de pó: {slot['qty']:.10f} val=${slot_value_usd:.4f}")
                              engine.holdings[symbol] = max(0, engine.holdings.get(symbol, 0) - slot["qty"])
                              slot.update({"qty": 0.0, "entry": 0.0, "peak": 0.0, "pyramids": 0, "be_sl": 0.0})
                              continue

                          slot["peak"] = max(slot["peak"], price)
                          gain_pct = (price - slot["entry"]) / slot["entry"] * 100

                          # ── ATR Stop Loss dinâmico ──────────────────────────────
                          # Usa ATR do 1H para SL baseado em volatilidade real do ativo
                          # Substitui o percentual fixo por distância proporcional ao ruído
                          if current_atr > 0 and slot["entry"] > 0:
                              atr_sl_price = atr_stop_loss(
                                  slot["entry"], df_1h_regime,
                                  multiplier=2.0, min_pct=0.03, max_pct=0.12
                              )
                          else:
                              atr_sl_price = slot["entry"] * (1 - current_sl_pct / 100)

                          # Break-even stop: após +1.5%, SL sobe para o ponto de entrada
                          if gain_pct >= BREAKEVEN_ACTIVATE_PCT:
                              effective_sl = slot["entry"]
                          else:
                              effective_sl = atr_sl_price
                          effective_sl = max(effective_sl, slot.get("be_sl", 0))
                          slot["be_sl"] = effective_sl  # persiste o break-even stop

                          tp_hit = price >= slot["entry"] * (1 + current_tp_pct / 100)
                          sl_hit = price <= effective_sl
                          tr_act = gain_pct >= TRAILING_ACTIVATE_PCT
                          tr_hit = tr_act and price <= slot["peak"] * (1 - TRAILING_STOP_PCT / 100)

                          def _sell_slot(qty, label, is_sl=False):
                              net = qty * price * (1 - 0.006)
                              sold = engine.sell(symbol, qty, price, f"{strat.name}:{label}")
                              if sold:
                                  pnl = net - slot["entry"] * qty
                                  slot["realized"] += pnl
                                  _attr_pnl(strat.name, pnl)
                                  _record_trade("SELL", pair, qty, price, net, f"{strat.name}:{label}")
                                  logger.info(f"[{pair}][{strat.name}] SELL {label} gain={gain_pct:+.2f}% P&L ${pnl:+.2f}")
                                  if is_sl:
                                      sl_cooldowns[key] = SL_COOLDOWN_CYCLES
                                  _daily_trade_count[today_str] = _daily_trade_count.get(today_str, 0) + 1
                                  # Sempre insere novo entry no topo — mesma lógica do BUY executado
                                  state["feed"].insert(0, {
                                      "time": now_str, "cycle": state["cycle"],
                                      "pair": pair, "strategy": strat.name,
                                      "signal": "SELL", "price": price,
                                      "executed": True,
                                      "note": label,
                                  })
                                  state["feed"] = state["feed"][:100]
                                  # ← Só atualiza o slot SE a venda foi executada no engine
                                  rem = slot["qty"] - qty
                                  if rem < 1e-8:
                                      slot.update({"qty": 0.0, "entry": 0.0, "peak": 0.0, "pyramids": 0, "be_sl": 0.0})
                                  else:
                                      slot["qty"] = rem
                                      slot["peak"] = price  # reseta peak após venda parcial
                              else:
                                  logger.warning(f"[{pair}][{strat.name}] SELL {label} FALHOU — engine rejeitou (held insuficiente?)")

                          # SL apertado para posições com pyramid
                          pyramid_sl_hit = (slot.get("pyramids", 0) > 0 and gain_pct <= -1.5)

                          if tp_hit:
                              _sell_slot(slot["qty"], f"TP+{current_tp_pct:.0f}%")
                          elif sl_hit:
                              lbl = f"BE-stop" if gain_pct >= 0 else f"SL-{current_sl_pct:.1f}%"
                              _sell_slot(slot["qty"], lbl, is_sl=True)
                          elif pyramid_sl_hit:
                              _sell_slot(slot["qty"], f"SL-pyramid-1.5%", is_sl=True)
                          elif tr_hit:
                              _sell_slot(slot["qty"], f"TRAILING-{TRAILING_STOP_PCT:.1f}%")
                          # ── TP parcial (+2.5%) para estratégias com pyramid ──────
                          elif gain_pct >= 2.5 and slot.get("pyramids", 0) > 0:
                              half_qty = slot["qty"] / 2
                              half_usd = half_qty * price
                              if half_usd >= 1.0:   # mínimo $1 — evita trades de pó
                                  _sell_slot(half_qty, f"TP_HALF+2.5%")
                          elif extreme_greed or signal == "SELL":
                              # Saída gradual: TRADE_PCT% do patrimonio por ciclo
                              max_qty = portfolio_total * TRADE_PCT / price
                              sell_q  = min(slot["qty"], max_qty)
                              if sell_q > 1e-8:
                                  lbl = f"GREED{fg_value}" if extreme_greed else "SELL"
                                  _sell_slot(sell_q, lbl)
                          elif signal == "BUY" and gain_pct >= PYRAMID_MIN_GAIN_PCT:
                              pdone = slot.get("pyramids", 0)
                              if pdone < PYRAMID_MAX:
                                  # Pyramid proporcional à entrada original — não varia com dynamic_pct atual
                                  base_usd = slot.get("entry_usd") or (portfolio_total * dynamic_pct)
                                  pyr_usd  = base_usd * PYRAMID_SIZE_PCT
                                  if engine.balance_usd < pyr_usd * 1.006:
                                      pyr_usd = max(0, engine.balance_usd - (engine.balance_usd * 0.006))

                                  if pyr_usd > 1.0:  # Mínimo de $1 para pyramid
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
                                          _count_buy(strat.name)
                                          logger.info(f"[{pair}][{strat.name}] 📈 PYRAMID #{pdone+1} "
                                                      f"(gain {gain_pct:.1f}%)")

                          slot["unrealized"] = (price - slot["entry"]) * slot["qty"] if slot["qty"] > 0 else 0.0

                      elif signal == "BUY" and not extreme_greed:
                          # ── Filtros de qualidade antes de executar BUY ─────────
                          sl_cd = sl_cooldowns.get(key, 0)
                          if sl_cd > 0:
                              sl_cooldowns[key] = sl_cd - 1
                              logger.debug(f"[{pair}][{strat.name}] BUY bloqueado — SL cooldown ({sl_cd})")

                          elif circuit_open:
                              logger.info(f"[{pair}][{strat.name}] BUY bloqueado — circuit breaker ({daily_trades}/{MAX_DAILY_TRADES} trades hoje)")

                          elif open_slots_count >= MAX_OPEN_SLOTS:
                              logger.info(f"[{pair}][{strat.name}] BUY bloqueado — max slots abertos ({open_slots_count}/{MAX_OPEN_SLOTS})")

                          elif time.time() - last_buy_time.get(key, 0) < BUY_COOLDOWN_SECONDS:
                              logger.debug(f"[{pair}][{strat.name}] BUY bloqueado — cooldown 1h ativo")

                          elif not mtf_bullish and strat.name in ("EMA Pullback", "Donchian Breakout"):
                              # MTF: Donchian e EMA só operam se 6H confirma uptrend
                              # MACD e Stoch ficam livres do filtro MTF (mais oportunidades)
                              logger.info(f"[{pair}][{strat.name}] BUY bloqueado — MTF 6H bearish")

                          elif market_regime == "ranging" and strat.name == "Donchian Breakout":
                              # ADX < 20: só bloqueia Donchian (mais propenso a bull traps laterais)
                              # EMA Pullback pode operar em ranging (pullbacks funcionam em laterais)
                              logger.debug(f"[{pair}][{strat.name}] BUY bloqueado — regime ranging (ADX<20)")

                          else:
                              # ── Todos os filtros passaram: executa o BUY ────────
                              trade_usd = portfolio_total * dynamic_pct
                              if engine.balance_usd < trade_usd * 1.006:
                                  trade_usd = max(0, engine.balance_usd - (engine.balance_usd * 0.006))

                              if trade_usd > 1.0:
                                  qty = trade_usd / price
                                  if engine.buy(symbol, trade_usd, price, strat.name):
                                      slot["qty"]       = qty
                                      slot["entry"]     = price
                                      slot["peak"]      = price
                                      slot["pyramids"]  = 0
                                      slot["entry_usd"] = trade_usd
                                      last_buy_time[key] = time.time()
                                      _daily_trade_count[today_str] = daily_trades + 1
                                      _record_trade("BUY", pair, qty, price, trade_usd, strat.name)
                                      _count_buy(strat.name)
                                      pct_used = (trade_usd / engine.initial_balance) * 100 if engine.initial_balance > 0 else 0
                                      logger.info(f"[{pair}][{strat.name}] ✅ BUY {pct_used:.1f}% "
                                                  f"R${trade_usd*usd_brl:.0f} @ ${price:,.2f} "
                                                  f"[regime={market_regime} mtf={mtf_bullish}]")
                                      state["feed"].insert(0, {
                                          "time": now_str, "cycle": state["cycle"],
                                          "pair": pair, "strategy": strat.name,
                                          "signal": "BUY", "price": price,
                                          "executed": True,
                                          "note": f"R${trade_usd*usd_brl:.0f} ({pct_used:.1f}% PL)",
                                      })
                                      state["feed"] = state["feed"][:100]
                              else:
                                  logger.info(f"[{pair}][{strat.name}] BUY negado — saldo insuficiente (< $1)")

                # ── Slot manual: SL/TP/Trailing (fecha 100%) ──────────────────
                ms = strategy_slots.get(f"manual:{pair}")
                if ms and ms.get("qty", 0) > 0:
                    ms["peak"] = max(ms["peak"], price)
                    g   = (price - ms["entry"]) / ms["entry"] * 100
                    # Break-even stop para manual também
                    ms_eff_sl = ms["entry"] if g >= BREAKEVEN_ACTIVATE_PCT else ms["entry"] * (1 - current_sl_pct / 100)
                    ms_eff_sl = max(ms_eff_sl, ms.get("be_sl", 0))
                    ms["be_sl"] = ms_eff_sl
                    tph = price >= ms["entry"] * (1 + current_tp_pct / 100)
                    slh = price <= ms_eff_sl
                    tra = g >= TRAILING_ACTIVATE_PCT
                    trh = tra and price <= ms["peak"] * (1 - TRAILING_STOP_PCT / 100)
                    rsn = (f"TP+{current_tp_pct:.0f}%" if tph else
                           f"BE-stop"                   if slh and g >= 0 else
                           f"SL-{current_sl_pct:.1f}%"  if slh else
                           f"TRAILING-{TRAILING_STOP_PCT:.1f}%" if trh else None)
                    if rsn:
                        net = ms["qty"] * price * (1 - 0.006)
                        if engine.sell(symbol, ms["qty"], price, f"manual:{rsn}"):
                            ms["realized"] += net - ms["entry"] * ms["qty"]
                            _record_trade("SELL", pair, ms["qty"], price, net, f"manual:{rsn}")
                            logger.info(f"[{pair}][manual] {rsn} @ ${price:,.2f}")
                            # ← slot só reseta se venda foi executada
                            ms.update({"qty": 0.0, "entry": 0.0, "peak": 0.0, "be_sl": 0.0})
                        else:
                            logger.warning(f"[{pair}][manual] {rsn} FALHOU — engine rejeitou")
                    else:
                        ms["unrealized"] = (price - ms["entry"]) * ms["qty"]

                # ── Verificação de consistência slots ↔ engine ───────────────
                # Garante que slots não acumulem qty quando engine não tem posição
                held_in_engine = engine.holdings.get(symbol, 0)
                slots_total_qty = sum(
                    strategy_slots.get(f"{s.name}:{pair}", {}).get("qty", 0)
                    for s in all_strategies
                ) + strategy_slots.get(f"manual:{pair}", {}).get("qty", 0)

                if held_in_engine < slots_total_qty - 1e-6:
                    # Engine tem menos que os slots declaram — corrige proporcionalmente
                    logger.warning(f"[{pair}] INCONSISTÊNCIA: engine={held_in_engine:.6f} slots={slots_total_qty:.6f} — corrigindo")
                    if slots_total_qty > 1e-10:
                        ratio = held_in_engine / slots_total_qty
                        for s in all_strategies:
                            sk = f"{s.name}:{pair}"
                            if strategy_slots.get(sk, {}).get("qty", 0) > 0:
                                strategy_slots[sk]["qty"] *= ratio
                                if strategy_slots[sk]["qty"] < 1e-8:
                                    strategy_slots[sk].update({"qty": 0.0, "entry": 0.0, "peak": 0.0, "pyramids": 0, "be_sl": 0.0})
                        ms_key = f"manual:{pair}"
                        if strategy_slots.get(ms_key, {}).get("qty", 0) > 0:
                            strategy_slots[ms_key]["qty"] *= ratio
                            if strategy_slots[ms_key]["qty"] < 1e-8:
                                strategy_slots[ms_key].update({"qty": 0.0, "entry": 0.0, "peak": 0.0, "be_sl": 0.0})

                # ── Salva slots e atualiza signals no state ───────────────────
                _save_slots(strategy_slots)
                state["slots"] = strategy_slots

                rsi_val     = get_rsi_value(candles_1h)
                entry_price = engine.entry_prices.get(symbol)
                change_pct  = ((price - entry_price) / entry_price * 100) if entry_price else None
                # ATR SL level para o frontend exibir
                atr_sl_price = None
                atr_sl_pct_val = None
                if entry_price and current_atr > 0:
                    _atr_sl = atr_stop_loss(entry_price, df_1h_regime,
                                            multiplier=2.0, min_pct=0.03, max_pct=0.12)
                    atr_sl_price   = round(_atr_sl, 4)
                    atr_sl_pct_val = round((entry_price - _atr_sl) / entry_price * 100, 2)

                state["signals"][pair] = {
                    "strategies":  pair_signals,
                    "trend":       trend,
                    "vol_guard":   vol_signal,
                    "rsi":         rsi_val,
                    "entry_price": round(entry_price, 2) if entry_price else None,
                    "change_pct":  round(change_pct,  2) if change_pct is not None else None,
                    "sl_level":    round(entry_price * (1 - current_sl_pct / 100), 2) if entry_price else None,
                    "tp_level":    round(entry_price * (1 + current_tp_pct / 100), 2) if entry_price else None,
                    # Novos campos para o dashboard
                    "regime":      market_regime,           # 'trending' | 'ranging' | 'neutral'
                    "mtf_ok":      mtf_bullish,             # True/False — EMA50 > EMA200 no 6H
                    "atr_sl_level": atr_sl_price,           # preço do ATR stop
                    "atr_sl_pct":  atr_sl_pct_val,          # % de distância do ATR stop
                    "atr_value":   round(current_atr, 6) if current_atr else None,
                }

                # Trades de hoje para circuit breaker no frontend
                state["trades_today"] = _daily_trade_count.get(now_str[:10], 0)
                state["max_daily_trades"] = MAX_DAILY_TRADES
                state["open_slots_count"] = open_slots_count
                state["max_open_slots"]   = MAX_OPEN_SLOTS
                log_cycle(logger, state["cycle"], pair, price, pair_signals, trend)

            except Exception as e:
                logger.error(f"[{pair}] Erro: {e}")

        total, pnl = _update_portfolio_state()
        log_portfolio(logger, engine.balance_usd, total, pnl,
                      (pnl / engine.initial_balance) * 100, engine.holdings)
        state["history"].append({"time": now_str, "ts": int(time.time()), "total": round(total, 2)})
        state["history"] = state["history"][-90000:]
        _save_history(state["history"])

        # Atualizar KPIs e tamanho médio de trade a cada ciclo
        state["kpis"] = _calculate_kpis()
        if _dynamic_pcts:
            state["trade_pct"] = round(sum(_dynamic_pcts) / len(_dynamic_pcts), 4)

        await broadcast(state)
        await asyncio.sleep(CYCLE_INTERVAL)


@app.on_event("startup")
async def startup():
    # ── Sincronizar initial_balance com cotação BRL atual ──────────────────
    # Força busca nova ignorando cache (ts=0) para garantir cotação real no startup
    _usd_brl_cache["ts"] = 0.0
    usd_brl_now = _fetch_usd_brl()
    correct_initial_usd = TOTAL_BRL_INITIAL / usd_brl_now
    logger.info(f"[STARTUP] USD/BRL={usd_brl_now:.4f} → initial_balance={correct_initial_usd:.4f} USD (R${TOTAL_BRL_INITIAL})")
    engine.initial_balance = correct_initial_usd
    # Ajusta balance_usd se não houver posições abertas (sessão limpa)
    if not engine.holdings:
        engine.balance_usd = correct_initial_usd
    engine._save_state()
    state["usd_brl"] = usd_brl_now

    # Inicializa preços antes de iniciar trading loop
    logger.info(f"[STARTUP] Iniciando inicialização de preços para {PAIRS}")
    for pair in PAIRS:
        try:
            ticker = client.get_ticker(pair)
            price = float(ticker.get("price", 0))
            if price:
                state["prices"][pair] = {
                    "price": price,
                    "price_pct_chg": float(ticker.get("price_percentage_change_24h", 0)),
                    "volume_24h": float(ticker.get("volume_24h", 0)),
                }
                engine.update_price(pair.split("-")[0], price)
                logger.info(f"[STARTUP] {pair}: ${price:.2f}")
            else:
                logger.warning(f"[STARTUP] Preço inválido para {pair}: {ticker.get('price')}")
        except Exception as e:
            logger.error(f"[STARTUP] Erro ao buscar {pair}: {type(e).__name__}: {e}")

    # ── Fix Bug 4: Atualizar portfolio state ANTES do primeiro ciclo ──────
    _update_portfolio_state()
    logger.info(f"[STARTUP] Portfolio inicializado — USD: ${engine.balance_usd:.2f} | Total: ${engine.portfolio_value():.2f} | initial_balance: ${engine.initial_balance:.2f}")
    logger.info(f"[STARTUP] state['prices'] após inicialização: {list(state['prices'].keys())}")
    asyncio.create_task(trading_loop())
