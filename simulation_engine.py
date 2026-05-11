import pandas as pd
import numpy as np
import os
from datetime import datetime
from strategies.donchian_breakout import DonchianBreakout
from strategies.ema_pullback import EMAPullback
from strategies.macd_momentum import MACDMomentum
from strategies.volatility_guard import VolatilityGuard
from strategies.trend_filter import TrendFilter
from strategies.market_regime import calc_adx

# Configuration matching dashboard/app.py
PAIRS = ["BTC-USD", "ETH-USD", "SOL-USD", "AVAX-USD", "LINK-USD", "DOGE-USD"]
INITIAL_BRL = 5000.0
TRADE_PCT = 0.10
TAKER_FEE = 0.006
MAX_OPEN_SLOTS = 4
MAX_DAILY_TRADES = 10
BUY_COOLDOWN_SECONDS = 10800  # 3h

SL_MIN = 3.0
SL_MAX = 7.0

PAIR_SL_RANGE = {
    "BTC-USD":    (0.02, 0.04),
    "ETH-USD":    (0.03, 0.05),
    "SOL-USD":    (0.05, 0.07),
    "AVAX-USD":   (0.05, 0.07),
    "LINK-USD":   (0.05, 0.07),
    "DOGE-USD":   (0.05, 0.08),
}

PAIR_TRAILING = {
    "BTC-USD":    (4.0, 5.0),
    "ETH-USD":    (4.0, 5.0),
    "SOL-USD":    (6.0, 7.0),
    "AVAX-USD":   (6.0, 7.0),
    "LINK-USD":   (6.0, 7.0),
    "DOGE-USD":   (6.0, 7.0),
}

PAIR_BREAKEVEN = {
    "BTC-USD":    3.0,
    "ETH-USD":    3.0,
    "SOL-USD":    4.5,
    "AVAX-USD":   4.5,
    "LINK-USD":   4.5,
    "DOGE-USD":   4.5,
}

PYRAMID_MAX = 1
PYRAMID_MIN_GAIN_PCT = 3.0
PYRAMID_SIZE_PCT = 0.25

class SimulationEngine:
    def __init__(self):
        self.balance_usd = 0.0
        self.initial_balance_usd = 0.0
        self.holdings = {p: 0.0 for p in PAIRS}
        self.slots = {} # f"{strat}:{pair}"
        self.trades = []
        self.daily_trade_count = {} # date_str: count
        self.last_buy_time = {} # f"{strat}:{pair}": timestamp
        self.prices = {p: 0.0 for p in PAIRS}
        self.data_1h = {}
        self.data_1d = {}
        self.brl_usd_h = None

        self.strategies = [
            DonchianBreakout(),
            EMAPullback(),
            MACDMomentum()
        ]
        self.vol_guard = VolatilityGuard(threshold_pct=25.0, consecutive_days=3)
        self.trend_filter = TrendFilter(period=50)

        # Performance tracking
        self.closed_trades_pnl = [] # list of (pnl_usd, is_win)

    def load_data(self):
        for pair in PAIRS:
            # yfinance MultiIndex CSV structure
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

        # Initial conversion
        first_brl = self.brl_usd_h.iloc[0]
        self.balance_usd = INITIAL_BRL / first_brl
        self.initial_balance_usd = self.balance_usd

    def get_market_mode(self, btc_1h, btc_1d):
        if len(btc_1d) < 50: return "chop"
        close_d = btc_1d['Close']
        ema200_d = close_d.ewm(span=200, adjust=False).mean().iloc[-1]
        price = btc_1h['Close'].iloc[-1]
        if price < ema200_d: return "bear"

        # Strategies and calc_adx expect lowercase columns
        h_data_lower = btc_1h.tail(100).rename(columns=str.lower)
        adx_val = calc_adx(h_data_lower)
        if adx_val > 25: return "bull"
        return "chop"

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
            if i < 150: continue # Sufficient history for all indicators

            date_key = ts.strftime("%Y-%m-%d")
            self.daily_trade_count.setdefault(date_key, 0)

            # Current USDBRL
            current_brl_rate = self.brl_usd_h.asof(ts)
            if np.isnan(current_brl_rate): current_brl_rate = 5.0

            btc_h_slice = self.data_1h["BTC-USD"][:ts]
            btc_d_slice = self.data_1d["BTC-USD"][:ts]
            market_mode = self.get_market_mode(btc_h_slice, btc_d_slice)

            fg_value = 50
            current_sl_pct = 5.0
            current_tp_pct = 8.0
            if market_mode == "bull": current_tp_pct = 14.0
            elif market_mode == "bear": current_tp_pct = 5.0

            open_slots_count = sum(1 for s in self.slots.values() if s['qty'] > 1e-8)

            for pair in PAIRS:
                symbol = pair.split("-")[0]
                h_data = self.data_1h[pair][:ts]
                d_data = self.data_1d[pair][:ts]
                if h_data.empty: continue
                price = h_data['Close'].iloc[-1]
                self.prices[pair] = price

                # Vol Guard (daily)
                vol_signal = self.vol_guard.analyze(self.convert_to_strat_df(d_data))
                if vol_signal == "SELL":
                    self.liquidate_pair(pair, price, "VolGuard", ts)
                    continue

                for strat in self.strategies:
                    key = f"{strat.name}:{pair}"
                    slot = self.slots.get(key, {"qty": 0.0, "entry": 0.0, "peak": 0.0, "pyramids": 0, "be_sl": 0.0, "cost_basis": 0.0})

                    if slot["qty"] > 1e-8:
                        slot["peak"] = max(slot["peak"], price)
                        gain_pct = (price - slot["entry"]) / slot["entry"] * 100

                        be_pct = PAIR_BREAKEVEN.get(pair, 3.0)
                        eff_sl = slot["entry"] if gain_pct >= be_pct else slot["entry"] * (1 - current_sl_pct / 100)
                        eff_sl = max(eff_sl, slot.get("be_sl", 0))
                        slot["be_sl"] = eff_sl

                        tp_hit = price >= slot["entry"] * (1 + current_tp_pct / 100)
                        sl_hit = price <= eff_sl
                        tr_act_pct, tr_stop_pct = PAIR_TRAILING.get(pair, (4.0, 5.0))
                        tr_hit = gain_pct >= tr_act_pct and price <= slot["peak"] * (1 - tr_stop_pct / 100)

                        if tp_hit or sl_hit or tr_hit:
                            reason = "TP" if tp_hit else ("SL" if sl_hit else "Trailing")
                            self.sell(pair, slot["qty"], price, f"{strat.name}:{reason}", key, ts)
                        elif gain_pct >= PYRAMID_MIN_GAIN_PCT and slot["pyramids"] < PYRAMID_MAX:
                            h_data_lower = h_data.tail(100).rename(columns=str.lower)
                            adx_v = calc_adx(h_data_lower)
                            if adx_v > 25:
                                pyr_usd = slot["entry_usd"] * PYRAMID_SIZE_PCT
                                self.buy(pair, pyr_usd, price, f"{strat.name}:pyramid", key, ts, is_pyramid=True)
                    else:
                        if market_mode == "bear": continue
                        if self.daily_trade_count[date_key] >= MAX_DAILY_TRADES: continue
                        if open_slots_count >= MAX_OPEN_SLOTS: continue

                        last_buy = self.last_buy_time.get(key, 0)
                        if (ts.timestamp() - last_buy) < BUY_COOLDOWN_SECONDS: continue

                        signal = strat.analyze(self.convert_to_strat_df(h_data))
                        if signal == "BUY":
                            trend_sig = self.trend_filter.analyze(self.convert_to_strat_df(h_data))
                            if trend_sig != "BUY": continue

                            portfolio_value = self.balance_usd + sum(self.holdings[p]*self.prices.get(p,0) for p in PAIRS)
                            trade_usd = portfolio_value * TRADE_PCT
                            if self.buy(pair, trade_usd, price, strat.name, key, ts):
                                open_slots_count += 1
                                self.last_buy_time[key] = ts.timestamp()
                                self.daily_trade_count[date_key] += 1

        return self.generate_report()

    def buy(self, pair, usd_amount, price, strategy_name, slot_key, ts, is_pyramid=False):
        fee = usd_amount * TAKER_FEE
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

    def sell(self, pair, qty, price, strategy_name, slot_key, ts):
        gross = qty * price
        fee = gross * TAKER_FEE
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
        return True

    def liquidate_pair(self, pair, price, reason, ts):
        for key in list(self.slots.keys()):
            if key.endswith(f":{pair}"):
                slot = self.slots[key]
                if slot['qty'] > 1e-8:
                    self.sell(pair, slot['qty'], price, reason, key, ts)

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

        report = f"""# Relatório de Simulação de Trading Bot

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
    engine = SimulationEngine()
    result = engine.run()
    with open("relatorio.md", "w") as f:
        f.write(result)
    print("Simulação concluída. Relatório gerado em relatorio.md")
