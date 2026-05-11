import pandas as pd
import numpy as np
import os
from datetime import datetime, timedelta
from strategies.donchian_breakout import DonchianBreakout
from strategies.ema_pullback import EMAPullback
from strategies.macd_momentum import MACDMomentum
from strategies.volatility_guard import VolatilityGuard
from strategies.trend_filter import TrendFilter
from strategies.market_regime import calc_adx, calc_atr

# Configuration matching latest dashboard/app.py
PAIRS = ["BTC-USD", "ETH-USD", "SOL-USD", "AVAX-USD", "LINK-USD", "DOGE-USD"]
INITIAL_BRL = 5000.0
TRADE_PCT = 0.10  # Base size 10%
# Fee Tiers from app.py
COINBASE_FEE_TIERS = [
    (          0,  0.0060, 0.0120),  # $0–$10K
    (     10_000,  0.0035, 0.0080),  # $10K–$50K
]

SL_MIN = 3.0
SL_MAX = 7.0
TAKE_PROFIT_MIN = 5.0
TAKE_PROFIT_MAX = 18.0

PAIR_SL_RANGE = {
    "BTC-USD":    (0.02, 0.04),
    "ETH-USD":    (0.03, 0.05),
    "SOL-USD":    (0.05, 0.07),
    "AVAX-USD":   (0.05, 0.07),
    "LINK-USD":   (0.05, 0.07),
    "DOGE-USD":   (0.06, 0.09),
}

PAIR_TRAILING = {
    "BTC-USD":    (4.0, 5.0),
    "ETH-USD":    (4.0, 5.0),
    "SOL-USD":    (6.0, 7.0),
    "AVAX-USD":   (6.0, 7.0),
    "LINK-USD":   (6.0, 7.0),
    "DOGE-USD":   (6.0, 8.0),
}

PAIR_BREAKEVEN = {
    "BTC-USD":    3.0,
    "ETH-USD":    3.0,
    "SOL-USD":    4.5,
    "AVAX-USD":   4.5,
    "LINK-USD":   4.5,
    "DOGE-USD":   5.0,
}

STRATEGY_WEIGHTS = {
    "bull": {"Donchian Breakout": 1.5, "EMA Pullback": 1.3, "MACD Momentum": 1.2},
    "chop":  {"Donchian Breakout": 0.5, "EMA Pullback": 0.9, "MACD Momentum": 0.8},
    "bear":  {"Donchian Breakout": 1.0, "EMA Pullback": 1.0, "MACD Momentum": 1.0},
}

MAX_OPEN_SLOTS = 4
MAX_DAILY_TRADES = 10
BUY_COOLDOWN_SECONDS = 10800

PYRAMID_MAX = 1
PYRAMID_MIN_GAIN_PCT = 3.0
PYRAMID_SIZE_PCT = 0.25

class SimulationEngineV2:
    def __init__(self):
        self.balance_usd = 0.0
        self.initial_balance_usd = 0.0
        self.holdings = {p: 0.0 for p in PAIRS}
        self.slots = {}
        self.trades = []
        self.daily_trade_count = {}
        self.last_buy_time = {}
        self.prices = {p: 0.0 for p in PAIRS}
        self.data_1h = {}
        self.data_1d = {}
        self.brl_usd_h = None
        self.total_volume_30d = 0.0

        self.strategies = [
            DonchianBreakout(),
            EMAPullback(),
            MACDMomentum()
        ]
        self.vol_guard = VolatilityGuard(threshold_pct=25.0, consecutive_days=3)
        self.trend_filter = TrendFilter(period=50)
        self.closed_trades_pnl = []

    def get_current_fees(self):
        if self.total_volume_30d < 10000:
            return 0.0060, 0.0120
        return 0.0035, 0.0080

    def load_data(self):
        for pair in PAIRS:
            df_1h = pd.read_csv(f"data_sim/{pair}_1h.csv", header=[0, 1], index_col=0)
            df_1h.columns = df_1h.columns.get_level_values(0)
            df_1h.index = pd.to_datetime(df_1h.index, utc=True)
            self.data_1h[pair] = df_1h

            df_1d = pd.read_csv(f"data_sim/{pair}_1d.csv", header=[0, 1], index_col=0)
            df_1d.columns = df_1d.columns.get_level_values(0)
            df_1d.index = pd.to_datetime(df_1d.index, utc=True)
            self.data_1d[pair] = df_1d

        brl_h = pd.read_csv("data_sim/USDBRL_1h.csv", header=[0, 1], index_col=0)
        brl_h.columns = brl_h.columns.get_level_values(0)
        brl_h.index = pd.to_datetime(brl_h.index, utc=True)
        self.brl_usd_h = brl_h['Close']

        first_brl = self.brl_usd_h.iloc[0]
        self.balance_usd = INITIAL_BRL / first_brl
        self.initial_balance_usd = self.balance_usd

    def get_market_mode(self, btc_1h, btc_1d):
        if len(btc_1d) < 50: return "chop"
        close_d = btc_1d['Close']
        ema200_d = close_d.ewm(span=200, adjust=False).mean().iloc[-1]
        price = btc_1h['Close'].iloc[-1]
        if price < ema200_d: return "bear"

        h_data_lower = btc_1h.tail(100).rename(columns=str.lower)
        adx_val = calc_adx(h_data_lower)
        if adx_val > 25: return "bull"
        return "chop"

    def calc_confidence_score(self, signals, regime, adx):
        weights = STRATEGY_WEIGHTS.get(regime, STRATEGY_WEIGHTS["chop"])
        max_w = sum(weights.values())
        buy_score = sum(weights.get(s, 1.0) for s, sig in signals.items() if sig == "BUY")
        normalized = buy_score / max_w
        if regime == "bull" and adx > 20:
            normalized = min(1.0, normalized * (1 + min(0.25, (adx - 20) / 80)))
        return normalized

    def calculate_dynamic_position_size(self, pair, candles):
        if len(candles) < 20: return TRADE_PCT
        df = candles.tail(20).rename(columns=str.lower)
        df["tr"] = df["high"] - df["low"]
        atr_current = df["tr"].iloc[-1]
        atr_avg = df["tr"].tail(14).mean()
        vol_ratio = atr_avg / atr_current if atr_current > 0 else 1.0
        size = TRADE_PCT * vol_ratio
        return max(0.02, min(TRADE_PCT, size))

    def convert_to_strat_df(self, df):
        out = df.reset_index()
        res = pd.DataFrame()
        res['start'] = out.iloc[:, 0].astype(np.int64) // 10**9
        res['low'] = out['Low']
        res['high'] = out['High']
        res['open'] = out['Open']
        res['close'] = out['Close']
        res['volume'] = out['Volume']
        return res

    def run(self):
        self.load_data()
        timeline = self.data_1h["BTC-USD"].index

        for i, ts in enumerate(timeline):
            if i < 150: continue

            date_key = ts.strftime("%Y-%m-%d")
            self.daily_trade_count.setdefault(date_key, 0)

            cutoff_30d = ts - timedelta(days=30)
            self.total_volume_30d = sum(t['usd'] for t in self.trades if t['ts'] >= cutoff_30d)
            maker_fee, taker_fee = self.get_current_fees()

            btc_h_slice = self.data_1h["BTC-USD"][:ts]
            btc_d_slice = self.data_1d["BTC-USD"][:ts]
            market_mode = self.get_market_mode(btc_h_slice, btc_d_slice)

            fg_value = 50
            if fg_value >= 70: current_tp_pct = 7.0
            elif market_mode == "bear": current_tp_pct = 5.0
            elif market_mode == "bull": current_tp_pct = 14.0
            else: current_tp_pct = 8.0

            current_sl_pct = 5.0

            open_slots_count = sum(1 for s in self.slots.values() if s['qty'] > 1e-8)

            for pair in PAIRS:
                symbol = pair.split("-")[0]
                h_data = self.data_1h[pair][:ts]
                d_data = self.data_1d[pair][:ts]
                if h_data.empty: continue
                price = h_data['Close'].iloc[-1]
                self.prices[pair] = price

                vol_signal = self.vol_guard.analyze(self.convert_to_strat_df(d_data))
                if vol_signal == "SELL":
                    self.liquidate_pair(pair, price, "VolGuard", ts, taker_fee)
                    continue

                signals_this_cycle = {}
                for strat in self.strategies:
                    signals_this_cycle[strat.name] = strat.analyze(self.convert_to_strat_df(h_data))

                h_data_lower = h_data.tail(100).rename(columns=str.lower)
                adx_val = calc_adx(h_data_lower)
                pair_score = self.calc_confidence_score(signals_this_cycle, market_mode, adx_val)
                dynamic_pct = self.calculate_dynamic_position_size(pair, h_data)

                for strat in self.strategies:
                    key = f"{strat.name}:{pair}"
                    slot = self.slots.get(key, {"qty": 0.0, "entry": 0.0, "peak": 0.0, "pyramids": 0, "be_sl": 0.0, "cost_basis": 0.0})
                    signal = signals_this_cycle[strat.name]

                    if slot["qty"] > 1e-8:
                        slot["peak"] = max(slot["peak"], price)
                        gain_pct = (price - slot["entry"]) / slot["entry"] * 100

                        be_pct = PAIR_BREAKEVEN.get(pair, 4.5)
                        eff_sl = slot["entry"] if gain_pct >= be_pct else slot["entry"] * (1 - current_sl_pct / 100)
                        eff_sl = max(eff_sl, slot.get("be_sl", 0))
                        slot["be_sl"] = eff_sl

                        tp_hit = price >= slot["entry"] * (1 + current_tp_pct / 100)
                        sl_hit = price <= eff_sl
                        tr_act_pct, tr_stop_pct = PAIR_TRAILING.get(pair, (6.0, 7.0))
                        tr_hit = gain_pct >= tr_act_pct and price <= slot["peak"] * (1 - tr_stop_pct / 100)

                        if tp_hit or sl_hit or tr_hit:
                            reason = "TP" if tp_hit else ("SL" if sl_hit else "Trailing")
                            self.sell(pair, slot["qty"], price, f"{strat.name}:{reason}", key, ts, taker_fee)
                        elif signal == "BUY" and gain_pct >= PYRAMID_MIN_GAIN_PCT and adx_val > 25:
                            if slot["pyramids"] < PYRAMID_MAX:
                                pyr_usd = slot["entry_usd"] * PYRAMID_SIZE_PCT
                                self.buy(pair, pyr_usd, price, f"{strat.name}:pyramid", key, ts, taker_fee, is_pyramid=True)
                    else:
                        if signal == "BUY":
                            if market_mode == "bear": continue
                            if self.daily_trade_count[date_key] >= MAX_DAILY_TRADES: continue
                            if open_slots_count >= MAX_OPEN_SLOTS: continue
                            if pair_score < 0.60: continue

                            last_buy = self.last_buy_time.get(key, 0)
                            if (ts.timestamp() - last_buy) < BUY_COOLDOWN_SECONDS: continue

                            trend_sig = self.trend_filter.analyze(self.convert_to_strat_df(h_data))
                            if trend_sig != "BUY": continue

                            if market_mode == "chop": score_mult = min(0.6, pair_score)
                            elif pair_score >= 0.85: score_mult = 1.4
                            elif pair_score >= 0.75: score_mult = 1.0
                            else: score_mult = 0.5

                            portfolio_value = self.balance_usd + sum(self.holdings[p]*self.prices.get(p,0) for p in PAIRS)
                            trade_usd = portfolio_value * min(TRADE_PCT, dynamic_pct * score_mult)

                            if self.buy(pair, trade_usd, price, strat.name, key, ts, taker_fee):
                                print(f"[{ts}] BUY {pair} at {price} via {strat.name}")
                                open_slots_count += 1
                                self.last_buy_time[key] = ts.timestamp()
                                self.daily_trade_count[date_key] += 1

        return self.generate_report()

    def buy(self, pair, usd_amount, price, strategy_name, slot_key, ts, fee_rate, is_pyramid=False):
        fee = usd_amount * fee_rate
        total_cost = usd_amount + fee
        if self.balance_usd < total_cost: return False

        qty = usd_amount / price
        self.balance_usd -= total_cost
        self.holdings[pair] += qty

        if not is_pyramid:
            self.slots[slot_key] = {
                "qty": qty, "entry": price, "peak": price,
                "pyramids": 0, "be_sl": 0.0, "entry_usd": usd_amount,
                "cost_basis": total_cost
            }
        else:
            slot = self.slots[slot_key]
            slot["cost_basis"] += total_cost
            total_qty = slot["qty"] + qty
            slot["entry"] = (slot["qty"]*slot["entry"] + qty*price) / total_qty
            slot["qty"] = total_qty
            slot["pyramids"] += 1

        self.trades.append({
            "ts": ts, "side": "BUY", "pair": pair, "qty": qty, "price": price,
            "usd": usd_amount, "fee": fee, "strat": strategy_name
        })
        return True

    def sell(self, pair, qty, price, strategy_name, slot_key, ts, fee_rate):
        gross = qty * price
        fee = gross * fee_rate
        net = gross - fee

        slot = self.slots[slot_key]
        pnl = net - slot["cost_basis"]
        self.closed_trades_pnl.append((pnl, pnl > 0))

        self.balance_usd += net
        self.holdings[pair] -= qty
        self.slots[slot_key] = {"qty": 0.0, "entry": 0.0, "peak": 0.0, "pyramids": 0, "be_sl": 0.0, "cost_basis": 0.0}

        self.trades.append({
            "ts": ts, "side": "SELL", "pair": pair, "qty": qty, "price": price,
            "usd": net, "fee": fee, "strat": strategy_name, "pnl": pnl
        })
        print(f"[{ts}] SELL {pair} at {price} via {strategy_name} P&L: {pnl}")
        return True

    def liquidate_pair(self, pair, price, reason, ts, fee_rate):
        for key in list(self.slots.keys()):
            if key.endswith(f":{pair}"):
                slot = self.slots[key]
                if slot['qty'] > 1e-8:
                    self.sell(pair, slot['qty'], price, reason, key, ts, fee_rate)

    def generate_report(self):
        final_equity_usd = self.balance_usd + sum(self.holdings[p]*self.prices.get(p,0) for p in PAIRS)
        final_brl_rate = self.brl_usd_h.iloc[-1]
        final_equity_brl = final_equity_usd * final_brl_rate

        total_trades = len(self.closed_trades_pnl)
        wins = sum(1 for pnl, win in self.closed_trades_pnl if win)
        win_rate = (wins / total_trades * 100) if total_trades > 0 else 0

        gross_profit = sum(pnl for pnl, win in self.closed_trades_pnl if win)
        gross_loss = abs(sum(pnl for pnl, win in self.closed_trades_pnl if not win))
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float('inf')

        report = f"""# Relatório de Simulação de Trading Bot (Versão V2 Atualizada)

## Resumo Executivo
- **Período:** 01/01/2026 a 30/04/2026
- **Portfolio Inicial:** R$ {INITIAL_BRL:,.2f}
- **Portfolio Final:** R$ {final_equity_brl:,.2f}
- **P&L Total:** R$ {final_equity_brl - INITIAL_BRL:,.2f} ({(final_equity_brl / INITIAL_BRL - 1)*100:+.2f}%)

## Métricas de Performance
- **Quantidade de Trades:** {total_trades}
- **Win Rate:** {win_rate:.2f}%
- **Profit Factor:** {profit_factor:.2f}

## Detalhes do Portfolio
- **Saldo Final em USD:** ${final_equity_usd:,.2f}
- **Cotação Final USD/BRL:** {final_brl_rate:.4f}
"""
        return report

if __name__ == "__main__":
    engine = SimulationEngineV2()
    result = engine.run()
    with open("relatorio.md", "w") as f:
        f.write(result)
    print("Simulação concluída com lógica v2. Relatório gerado em relatorio.md")
