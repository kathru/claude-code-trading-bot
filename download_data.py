import yfinance as yf
import pandas as pd
import os

PAIRS = ["BTC-USD", "ETH-USD", "SOL-USD", "AVAX-USD", "LINK-USD", "DOGE-USD"]
START_DATE = "2026-01-01"
END_DATE = "2026-05-01"  # Inclusive of April 30

def download():
    os.makedirs("data_sim", exist_ok=True)

    for pair in PAIRS:
        print(f"Downloading {pair}...")
        # 1H data
        df_1h = yf.download(pair, start=START_DATE, end=END_DATE, interval="1h")
        df_1h.to_csv(f"data_sim/{pair}_1h.csv")

        # 1D data
        df_1d = yf.download(pair, start=START_DATE, end=END_DATE, interval="1d")
        df_1d.to_csv(f"data_sim/{pair}_1d.csv")

    print("Downloading USD/BRL...")
    # BRL=X is the ticker for USD/BRL
    brl = yf.download("BRL=X", start=START_DATE, end=END_DATE, interval="1h")
    brl.to_csv("data_sim/USDBRL_1h.csv")

    brl_d = yf.download("BRL=X", start=START_DATE, end=END_DATE, interval="1d")
    brl_d.to_csv("data_sim/USDBRL_1d.csv")

if __name__ == "__main__":
    download()
