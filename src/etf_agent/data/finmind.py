"""FinMind API 用戶端：法人買賣超、月營收、股利、本益比。

免費版沒有 token 每小時約 300 次請求，註冊（免費）拿 token 後約 600 次。
把 token 設在環境變數 FINMIND_TOKEN。每檔股票每個資料集各快取一個 parquet，
之後只抓增量。
"""
from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass

import pandas as pd
import requests

from . import store

API_URL = "https://api.finmindtrade.com/api/v4/data"


@dataclass(frozen=True)
class Dataset:
    name: str  # FinMind dataset 名稱
    keys: tuple[str, ...]  # 去重用的欄位
    overlap_days: int  # 增量更新時往回重抓幾天（資料可能事後修正）


DATASETS = {
    "institutional": Dataset("TaiwanStockInstitutionalInvestorsBuySell", ("date", "name"), 7),
    "revenue": Dataset("TaiwanStockMonthRevenue", ("revenue_year", "revenue_month"), 70),
    "dividend": Dataset("TaiwanStockDividend", ("date", "year"), 400),
    "per": Dataset("TaiwanStockPER", ("date",), 7),
}


class FinMindError(RuntimeError):
    pass


class FinMindClient:
    def __init__(self, token: str | None = None, min_interval: float | None = None, max_wait_retries: int = 8):
        self.token = token if token is not None else os.environ.get("FINMIND_TOKEN", "")
        # 平均分攤到每小時上限之內
        self.min_interval = min_interval if min_interval is not None else (6.0 if self.token else 12.0)
        self.max_wait_retries = max_wait_retries
        self._last = 0.0
        self.session = requests.Session()
        if self.token:
            self.session.headers["Authorization"] = f"Bearer {self.token}"

    def _get(self, params: dict) -> pd.DataFrame:
        for attempt in range(self.max_wait_retries + 1):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                r = self.session.get(API_URL, params=params, timeout=60)
            except requests.RequestException as e:
                print(f"  連線錯誤 {e}，30 秒後重試", file=sys.stderr)
                time.sleep(30)
                continue
            if r.status_code == 402:  # 超過每小時額度
                if attempt >= self.max_wait_retries:
                    raise FinMindError("FinMind 每小時額度已用完")
                print("  FinMind 額度用完，等 10 分鐘再試", file=sys.stderr)
                time.sleep(600)
                continue
            j = r.json()
            if r.status_code != 200 or j.get("status") != 200:
                raise FinMindError(f"{params.get('dataset')} {params.get('data_id')}: {r.status_code} {j.get('msg')}")
            return pd.DataFrame(j.get("data") or [])
        raise FinMindError(f"重試 {self.max_wait_retries} 次仍失敗：{params}")

    def stock_info(self, refresh: bool = False) -> pd.DataFrame:
        cached = None if refresh else store.read_parquet(store.STOCK_INFO_PATH)
        if cached is not None:
            return cached
        df = self._get({"dataset": "TaiwanStockInfo"})
        store.write_parquet(df, store.STOCK_INFO_PATH)
        return df

    def fetch(self, key: str, stock_id: str, start: str, end: str | None = None) -> pd.DataFrame:
        ds = DATASETS[key]
        path = store.FINMIND_DIR / key / f"{stock_id}.parquet"
        cached = store.read_parquet(path)

        fetch_start = pd.Timestamp(start)
        if cached is not None and not cached.empty:
            last = pd.to_datetime(cached["date"]).max()
            fetch_start = max(fetch_start, last - pd.Timedelta(days=ds.overlap_days))

        params = {"dataset": ds.name, "data_id": stock_id, "start_date": fetch_start.date().isoformat()}
        if end:
            params["end_date"] = end
        new = self._get(params)

        if cached is not None and not cached.empty:
            df = pd.concat([cached, new], ignore_index=True)
        else:
            df = new
        if df.empty:
            return df
        df = df.drop_duplicates(subset=list(ds.keys), keep="last").sort_values(list(ds.keys)).reset_index(drop=True)
        store.write_parquet(df, path)
        return df

    def update_many(self, key: str, stock_ids: list[str], start: str) -> None:
        for i, sid in enumerate(stock_ids, 1):
            try:
                df = self.fetch(key, sid, start)
                print(f"  [{key}] {i}/{len(stock_ids)} {sid}: {len(df)} 筆")
            except FinMindError as e:
                print(f"  [{key}] {i}/{len(stock_ids)} {sid}: 失敗 {e}", file=sys.stderr)


def load_cached(key: str, stock_ids: list[str]) -> pd.DataFrame:
    """把快取裡多檔股票的同一資料集合併成一張長表。"""
    frames = []
    for sid in stock_ids:
        df = store.read_parquet(store.FINMIND_DIR / key / f"{sid}.parquet")
        if df is not None and not df.empty:
            frames.append(df)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    out["stock_id"] = out["stock_id"].astype(str)
    out["date"] = pd.to_datetime(out["date"])
    return out


def merge_into_cache(key: str, df: pd.DataFrame, stock_ids: list[str] | None = None) -> int:
    """把別的來源（例如交易所單日資料）整理成 FinMind 格式後併入每檔的快取，回傳更新的檔數。"""
    ds = DATASETS[key]
    if df.empty:
        return 0
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")  # 與 FinMind 快取一致
    if stock_ids is not None:
        df = df[df["stock_id"].isin(stock_ids)]
    n = 0
    for sid, new in df.groupby("stock_id"):
        path = store.FINMIND_DIR / key / f"{sid}.parquet"
        cached = store.read_parquet(path)
        merged = new if cached is None or cached.empty else pd.concat([cached, new], ignore_index=True)
        merged = merged.drop_duplicates(subset=list(ds.keys), keep="last").sort_values(list(ds.keys))
        store.write_parquet(merged.reset_index(drop=True), path)
        n += 1
    return n
