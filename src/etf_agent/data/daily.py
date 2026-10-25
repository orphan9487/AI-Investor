"""每日增量更新：比賽期間每天收盤後跑，幾分鐘內完成。

- 日 K：整段重抓（yfinance 批次很快）＋缺漏修補
- 法人買賣超：TWSE T86 + TPEx 三大法人，每個缺的交易日 2 次請求
- 月營收：TWSE/TPEx OpenAPI 最新一期，全市場 2 次請求
歷史資料仍由 scripts/update_data.py --finmind 一次抓齊。
"""
from __future__ import annotations

import sys

import pandas as pd

from . import store
from .exchange import latest_announcements, latest_monthly_revenue, tpex_institutional, twse_institutional
from .finmind import load_cached, merge_into_cache
from .prices import merge_official_vwap, update_official_vwap, update_prices
from .universe import load_universe


def update_institutional(stock_ids: list[str], dates: pd.DatetimeIndex, max_days: int = 15) -> list[pd.Timestamp]:
    """補齊 dates 中快取還沒有的交易日（最多往回 max_days 天），回傳實際補上的日期。"""
    cached = load_cached("institutional", stock_ids)
    have = set(cached["date"]) if not cached.empty else set()
    todo = [d for d in dates[-max_days:] if d not in have]
    done = []
    for d in todo:
        df = pd.concat([twse_institutional(d), tpex_institutional(d)], ignore_index=True)
        if df.empty:
            print(f"  法人 {d.date()}：交易所尚未公布", file=sys.stderr)
            continue
        merge_into_cache("institutional", df, stock_ids)
        done.append(d)
    return done


def update_revenue(stock_ids: list[str]) -> int:
    return merge_into_cache("revenue", latest_monthly_revenue(), stock_ids)


ANNOUNCEMENTS_PATH = store.DATA_DIR / "announcements.parquet"


def update_announcements(stock_ids: list[str]) -> int:
    """累積標的池公司的重大訊息，回傳新增筆數。"""
    new = latest_announcements()
    new = new[new["stock_id"].isin(stock_ids)]
    old = store.read_parquet(ANNOUNCEMENTS_PATH)
    df = new if old is None else pd.concat([old, new], ignore_index=True)
    before = 0 if old is None else len(old)
    df = df.drop_duplicates(subset=["stock_id", "date", "time", "subject"]).sort_values(["date", "time"])
    store.write_parquet(df.reset_index(drop=True), ANNOUNCEMENTS_PATH)
    return len(df) - before


def load_announcements() -> pd.DataFrame:
    df = store.read_parquet(ANNOUNCEMENTS_PATH)
    return pd.DataFrame(columns=["date", "time", "stock_id", "name", "subject"]) if df is None else df


def daily_update(start: str = "2021-01-01") -> dict:
    store.ensure_dirs()
    uni = load_universe()
    ids = uni["stock_id"].tolist()
    prices = update_prices(uni, start)
    dates = pd.DatetimeIndex(sorted(prices["date"].unique()))
    vwap_days = update_official_vwap(dates[-10:], set(ids))
    if vwap_days:
        store.write_parquet(merge_official_vwap(prices), store.PRICES_PATH)
    insti = update_institutional(ids, dates)
    rev = update_revenue(ids)
    ann = update_announcements(ids)
    return {
        "announcements_added": ann,
        "last_price_date": dates[-1].date().isoformat(),
        "official_vwap_days_added": [d.date().isoformat() for d in vwap_days],
        "institutional_days_added": [d.date().isoformat() for d in insti],
        "revenue_stocks_updated": rev,
    }
