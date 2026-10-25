"""標的池：2026/7 月底上市市值前 100 大 + 上櫃市值前 50 大。

官方清單在競賽官網「參考文件下載專區」。拿到之後存成 data/universe_official.csv
（至少要有 stock_id 欄位），load_universe() 就會優先使用官方清單。
在那之前，build_provisional_universe() 用 7/31 收盤價 × 發行股數自行估算。
"""
from __future__ import annotations

import re
import warnings

import pandas as pd

from . import store
from .exchange import get_json, to_num, tpex_daily_quotes, twse_daily_quotes

TWSE_BASIC_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"

COMMON_STOCK = re.compile(r"^[1-9]\d{3}$")  # 排除 ETF(00xx)、權證、特別股、TDR


def _twse_market_caps(asof: pd.Timestamp) -> pd.DataFrame:
    quotes = twse_daily_quotes(asof)
    if quotes.empty:
        raise RuntimeError(f"TWSE {asof.date()} 沒有行情（休市日？）")
    basic = get_json(TWSE_BASIC_URL)
    shares = pd.DataFrame({
        "stock_id": [b["公司代號"].strip() for b in basic],
        "shares": [to_num(b["已發行普通股數或TDR原股發行股數"]) for b in basic],
    })
    df = quotes[["stock_id", "name", "close"]].merge(shares, on="stock_id", how="inner")
    df["market"] = "twse"
    return df


def _tpex_market_caps(asof: pd.Timestamp) -> pd.DataFrame:
    df = tpex_daily_quotes(asof)
    if df.empty:
        raise RuntimeError(f"TPEx {asof.date()} 沒有行情（休市日？）")
    df = df[["stock_id", "name", "close", "shares"]].copy()
    df["market"] = "tpex"
    return df


def build_provisional_universe(asof: str = "2026-07-31", n_twse: int = 100, n_tpex: int = 50) -> pd.DataFrame:
    """用 asof 當日收盤價 × 發行股數估算市值並排名。

    注意：上市的發行股數取自 TWSE OpenAPI「目前」的公司基本資料，不是 7/31 當時的值；
    少數期間內有增資或減資的公司，排名可能和官方略有出入。
    """
    ts = pd.Timestamp(asof)
    parts = []
    for fetch, n in ((_twse_market_caps, n_twse), (_tpex_market_caps, n_tpex)):
        df = fetch(ts)
        df = df[df["stock_id"].str.match(COMMON_STOCK)]
        df = df.dropna(subset=["close", "shares"])
        df = df[(df["close"] > 0) & (df["shares"] > 0)].copy()
        df["market_cap"] = df["close"] * df["shares"]
        df = df.sort_values("market_cap", ascending=False).head(n)
        df["rank_in_market"] = range(1, len(df) + 1)
        parts.append(df)
    uni = pd.concat(parts, ignore_index=True)
    uni["asof"] = ts.date().isoformat()
    store.ensure_dirs()
    uni.to_csv(store.UNIVERSE_PROVISIONAL_PATH, index=False, encoding="utf-8-sig")
    return uni


def load_universe() -> pd.DataFrame:
    """回傳欄位至少含 stock_id, market, shares 的標的池。官方清單優先。"""
    prov = None
    if store.UNIVERSE_PROVISIONAL_PATH.exists():
        prov = pd.read_csv(store.UNIVERSE_PROVISIONAL_PATH, dtype={"stock_id": str})

    if store.UNIVERSE_OFFICIAL_PATH.exists():
        off = pd.read_csv(store.UNIVERSE_OFFICIAL_PATH, dtype={"stock_id": str})
        off["stock_id"] = off["stock_id"].str.strip()
        if prov is not None:
            extra = [c for c in ("market", "shares", "name") if c not in off.columns]
            if extra:
                off = off.merge(prov[["stock_id", *extra]], on="stock_id", how="left")
        if "market" not in off.columns or off["market"].isna().any():
            off = _fill_market_from_stock_info(off)
        off["source"] = "official"
        return off

    if prov is None:
        raise FileNotFoundError(
            "找不到標的池。請先執行 scripts/update_data.py，或把官方清單存成 "
            f"{store.UNIVERSE_OFFICIAL_PATH}"
        )
    warnings.warn("目前使用自行估算的暫定標的池，拿到官方清單後請存成 data/universe_official.csv", stacklevel=2)
    prov["source"] = "provisional"
    return prov


def _fill_market_from_stock_info(df: pd.DataFrame) -> pd.DataFrame:
    from .finmind import FinMindClient

    info = FinMindClient().stock_info()
    market = info.drop_duplicates("stock_id").set_index("stock_id")["type"]
    if "market" not in df.columns:
        df["market"] = pd.NA
    df["market"] = df["market"].fillna(df["stock_id"].map(market))
    if df["market"].isna().any():
        raise ValueError(f"無法判斷上市/上櫃：{df.loc[df['market'].isna(), 'stock_id'].tolist()}")
    return df
