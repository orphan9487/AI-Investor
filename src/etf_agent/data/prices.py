"""日 K 線：用 yfinance 一次批次下載整個標的池，再用 TWSE/TPEx 官方收盤價抽查。

存成長表 data/prices.parquet，欄位：
date, stock_id, open, high, low, close, adj_close, volume, vwap
open/high/low/close 是未還原的實際成交價；
vwap 是當日成交均價（比賽的成交價）：有官方資料用官方，否則以 (高+低+收)/3 近似；
adj_close 含除權息還原，只拿來算報酬類因子。
"""
from __future__ import annotations

import sys

import pandas as pd
import yfinance as yf

from . import store
from .exchange import get_json, to_num, tpex_daily_quotes, twse_daily_quotes

SUFFIX = {"twse": ".TW", "tpex": ".TWO"}
COLUMNS = ["date", "stock_id", "open", "high", "low", "close", "adj_close", "volume", "vwap"]
VWAP_OFFICIAL_PATH = store.DATA_DIR / "vwap_official.parquet"
SPLITS_PATH = store.DATA_DIR / "splits.parquet"


def split_factor(splits: pd.DataFrame | None, stock_id: str, date: pd.Timestamp) -> float:
    """yfinance 的價格已依「之後」發生的股票分割與配股調整；交易所的原始價格要除以這個倍數才能接得上。"""
    if splits is None or splits.empty:
        return 1.0
    s = splits[(splits["stock_id"] == stock_id) & (splits["date"] > date)]
    return float(s["ratio"].prod()) if len(s) else 1.0
BENCHMARKS = {"0050": "0050.TW"}  # 元大台灣50，當大盤參考

TWSE_LATEST_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TPEX_LATEST_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"


def _download(tickers: dict[str, str], start: str, end: str | None,
              splits_out: list | None = None) -> pd.DataFrame:
    """tickers: {stock_id: yahoo_ticker}。splits_out 若給 list，會附加 (date, stock_id, ratio)。"""
    raw = yf.download(
        list(tickers.values()),
        start=start,
        end=end,
        auto_adjust=False,
        actions=True,
        group_by="ticker",
        progress=False,
        threads=True,
    )
    frames = []
    for sid, tk in tickers.items():
        if tk not in raw.columns.get_level_values(0):
            print(f"  yfinance 沒有 {tk}", file=sys.stderr)
            continue
        df = raw[tk].rename(
            columns={"Open": "open", "High": "high", "Low": "low", "Close": "close",
                     "Adj Close": "adj_close", "Volume": "volume"}
        )
        if splits_out is not None and "Stock Splits" in df:
            sp = df["Stock Splits"]
            for d, v in sp[sp > 0].items():
                splits_out.append((pd.Timestamp(d).tz_localize(None).normalize(), sid, float(v)))
        df = df.dropna(subset=["close"])
        if df.empty:
            print(f"  {tk} 沒有資料", file=sys.stderr)
            continue
        df = df.reset_index().rename(columns={"Date": "date"})
        df["stock_id"] = sid
        df["vwap"] = (df["high"] + df["low"] + df["close"]) / 3  # 近似值，官方均價取得後覆蓋
        frames.append(df[COLUMNS])
    out = pd.concat(frames, ignore_index=True)
    out["date"] = pd.to_datetime(out["date"]).dt.tz_localize(None).dt.normalize()
    return out.sort_values(["date", "stock_id"]).reset_index(drop=True)


def _finmind_prices(stock_id: str, start: str, end: str | None) -> pd.DataFrame:
    from .finmind import FinMindClient, FinMindError

    params = {"dataset": "TaiwanStockPrice", "data_id": stock_id, "start_date": start}
    if end:
        params["end_date"] = end
    try:
        df = FinMindClient(min_interval=1.0, max_wait_retries=0)._get(params)
    except FinMindError as e:
        print(f"  {stock_id}：FinMind 補資料失敗（{e}），沿用 yfinance", file=sys.stderr)
        return pd.DataFrame()
    if df.empty:
        return df
    df = df.rename(columns={"max": "high", "min": "low", "Trading_Volume": "volume"})
    df["date"] = pd.to_datetime(df["date"])
    df["adj_close"] = df["close"]  # 免費版沒有還原股價；只影響報酬類因子
    df["vwap"] = df["Trading_money"] / df["volume"].where(df["volume"] > 0)
    return df[COLUMNS]


def trading_calendar(start: str, end: str | None = None) -> pd.DatetimeIndex:
    """歷史交易日（FinMind，快取在 data/calendar.parquet）＋ 快取之後用「平日扣掉 TWSE 休市日」延伸。
    每日流程不必再打 FinMind。"""
    from .exchange import twse_holidays
    from .finmind import FinMindClient, FinMindError

    path = store.DATA_DIR / "calendar.parquet"
    cached = store.read_parquet(path)
    if cached is None or pd.Timestamp(cached["date"].min()) > pd.Timestamp(start) + pd.Timedelta(days=7):
        try:
            df = FinMindClient(min_interval=1.0, max_wait_retries=0)._get(
                {"dataset": "TaiwanStockTradingDate", "start_date": start})
            cached = pd.DataFrame({"date": pd.to_datetime(df["date"])})
            store.write_parquet(cached, path)
        except FinMindError as e:
            if cached is None:
                raise
            print(f"  交易日曆沿用快取（FinMind：{e}）", file=sys.stderr)
    days = pd.DatetimeIndex(pd.to_datetime(cached["date"]))
    days = days[days >= pd.Timestamp(start)]
    stop = pd.Timestamp(end) if end else pd.Timestamp.today().normalize()
    if days.max() < stop:
        holidays = twse_holidays()
        ext = pd.bdate_range(days.max() + pd.Timedelta(days=1), stop)
        days = days.union(ext[~ext.isin(list(holidays))])
    return days[days <= stop]


def repair_bad_days(prices: pd.DataFrame, calendar: pd.DatetimeIndex, markets: dict[str, str],
                    splits: pd.DataFrame | None = None) -> pd.DataFrame:
    """yfinance 有時整天漏掉某個市場，或塞進「成交量 0、價格不動」的佔位資料。
    找出這些（日期, 市場），改用 TWSE/TPEx 官方單日行情取代；
    adj_close 用該股前後交易日的還原比例換算。

    markets: {stock_id: "twse" | "tpex"}
    """
    calendar = calendar[(calendar >= prices["date"].min()) & (calendar <= prices["date"].max())]
    first = prices.groupby("stock_id")["date"].min()
    last = prices.groupby("stock_id")["date"].max()
    traded = prices[prices["volume"] > 0]

    bad: list[tuple[pd.Timestamp, str]] = []
    for m in sorted(set(markets.values())):
        sids = [s for s, v in markets.items() if v == m and s in first.index]
        alive = pd.Series(0, index=calendar)
        for s in sids:
            alive += (calendar >= first[s]) & (calendar <= last[s])
        ok = traded[traded["stock_id"].isin(sids)].groupby("date")["stock_id"].nunique()
        ok = ok.reindex(calendar, fill_value=0)
        bad += [(d, m) for d in calendar if alive[d] > 0 and ok[d] < 0.5 * alive[d]]
    if not bad:
        return prices

    ratio_panel = (
        prices.assign(ratio=prices["adj_close"] / prices["close"])
        .pivot(index="date", columns="stock_id", values="ratio")
        .sort_index()
    )
    fetch = {"twse": twse_daily_quotes, "tpex": tpex_daily_quotes}
    no_data: dict[pd.Timestamp, set[str]] = {}
    for d, m in bad:
        sids = {s for s, v in markets.items() if v == m}
        whole = fetch[m](d)
        if whole.empty:  # 整個市場都查不到：休市
            no_data.setdefault(d, set()).add(m)
            continue
        q = whole[whole["stock_id"].isin(sids)].dropna(subset=["close"])
        q = q[q["volume"] > 0].copy()
        if q.empty:  # 市場有開、但這些股票沒成交（停牌）：維持原值
            print(f"  {d.date()} {m}：官方行情中這些股票也沒有成交（停牌），保留原值", file=sys.stderr)
            continue
        rp = ratio_panel.drop(index=d, errors="ignore")
        r = rp.reindex(rp.index.union([d])).bfill().ffill().loc[d]
        q["date"] = d
        # 交易所是原始價格，換算成與 yfinance 一致的分割／配股調整後基準
        f = q["stock_id"].map(lambda s: split_factor(splits, s, d))
        for col in ("open", "high", "low", "close", "vwap"):
            if col in q:
                q[col] = q[col] / f
        q["volume"] = q["volume"] * f
        q["adj_close"] = q["close"] * q["stock_id"].map(r).fillna(1.0)
        if "vwap" not in q:
            q["vwap"] = (q["high"] + q["low"] + q["close"]) / 3
        q["vwap"] = q["vwap"].fillna((q["high"] + q["low"] + q["close"]) / 3)
        replace = (prices["date"] == d) & prices["stock_id"].isin(q["stock_id"])
        prices = pd.concat([prices[~replace], q[prices.columns]], ignore_index=True)
        print(f"  {d.date()} {m}：yfinance 資料缺漏或為佔位值，改用交易所行情（{len(q)} 檔）", file=sys.stderr)

    # 所有市場的官方行情都沒有資料：臨時休市（颱風假等），整天移除
    all_markets = set(markets.values())
    closed = [d for d, ms in no_data.items() if ms >= all_markets]
    for d, ms in no_data.items():
        if d not in closed:
            print(f"  {d.date()} {sorted(ms)}：該市場查不到行情，保留原值", file=sys.stderr)
    if closed:
        print(f"  臨時休市，移除：{[d.date() for d in closed]}", file=sys.stderr)
        prices = prices[~prices["date"].isin(closed)]
    return prices.sort_values(["date", "stock_id"]).reset_index(drop=True)


def merge_official_vwap(prices: pd.DataFrame) -> pd.DataFrame:
    """用累積的官方成交均價（原始價格，換算成調整後基準）覆蓋近似值。"""
    off = store.read_parquet(VWAP_OFFICIAL_PATH)
    if off is None or off.empty:
        return prices
    splits = store.read_parquet(SPLITS_PATH)
    off = off.copy()
    off["vwap"] = off["vwap"] / [split_factor(splits, s, d) for s, d in zip(off["stock_id"], off["date"])]
    m = prices.merge(off[["date", "stock_id", "vwap"]], on=["date", "stock_id"], how="left", suffixes=("", "_off"))
    m["vwap"] = m["vwap_off"].fillna(m["vwap"])
    return m.drop(columns="vwap_off")


def update_official_vwap(dates: pd.DatetimeIndex, stock_ids: set[str]) -> list[pd.Timestamp]:
    """補抓 dates 中還沒有官方均價的交易日（每天 2 次請求），回傳補上的日期。"""
    off = store.read_parquet(VWAP_OFFICIAL_PATH)
    have = set(pd.to_datetime(off["date"])) if off is not None else set()
    added, frames = [], []
    for d in dates:
        if d in have:
            continue
        q = pd.concat([twse_daily_quotes(d), tpex_daily_quotes(d)], ignore_index=True)
        q = q[q["stock_id"].isin(stock_ids)].dropna(subset=["vwap"])
        if q.empty:
            continue
        q["date"] = d
        frames.append(q[["date", "stock_id", "vwap"]])
        added.append(d)
    if frames:
        parts = ([off] if off is not None else []) + frames
        new = pd.concat(parts, ignore_index=True)
        new = new.drop_duplicates(["date", "stock_id"], keep="last").sort_values(["date", "stock_id"])
        store.write_parquet(new.reset_index(drop=True), VWAP_OFFICIAL_PATH)
    return added


def update_prices(universe: pd.DataFrame, start: str, end: str | None = None) -> pd.DataFrame:
    tickers = {sid: sid + SUFFIX[m] for sid, m in zip(universe["stock_id"], universe["market"])}
    split_rows: list = []
    prices = _download(tickers, start, end, split_rows)
    splits = pd.DataFrame(split_rows, columns=["date", "stock_id", "ratio"])
    store.write_parquet(splits, SPLITS_PATH)

    # yfinance 偶爾缺很多天（轉上市櫃、下市、資料源錯誤），筆數偏少的改用 FinMind 比對
    counts = prices["stock_id"].value_counts().reindex(list(tickers), fill_value=0)
    suspect = counts[counts < 0.9 * counts.max()].index
    for sid in suspect:
        alt = _finmind_prices(sid, start, end)
        if len(alt) > 1.1 * counts[sid]:  # 只差幾筆不值得換掉還原股價
            print(f"  {sid}：yfinance {counts[sid]} 筆、FinMind {len(alt)} 筆，改用 FinMind（未還原股價）",
                  file=sys.stderr)
            prices = pd.concat([prices[prices["stock_id"] != sid], alt], ignore_index=True)
    calendar = trading_calendar(start, end)
    markets = dict(zip(universe["stock_id"], universe["market"]))
    prices = repair_bad_days(prices, calendar, markets, splits)
    off_calendar = sorted(set(prices["date"]) - set(calendar))
    if off_calendar:
        print(f"  移除非交易日資料：{[d.date() for d in off_calendar]}", file=sys.stderr)
        prices = prices[prices["date"].isin(calendar)]
    prices = merge_official_vwap(prices.sort_values(["date", "stock_id"]).reset_index(drop=True))

    missing = sorted(set(tickers) - set(prices["stock_id"]))
    if missing:
        print(f"  警告：{len(missing)} 檔沒有價格資料：{missing}", file=sys.stderr)
    last = prices["date"].max()
    stale = prices.groupby("stock_id")["date"].max()
    stale = stale[stale < last]
    if not stale.empty:
        print(f"  注意：{len(stale)} 檔最後交易日早於 {last.date()}（停牌或下市）：{stale.dt.date.to_dict()}",
              file=sys.stderr)
    store.write_parquet(prices, store.PRICES_PATH)

    bench = repair_bad_days(_download(BENCHMARKS, start, end), calendar, {s: "twse" for s in BENCHMARKS})
    store.write_parquet(bench, store.BENCHMARK_PATH)
    return prices


def load_prices() -> pd.DataFrame:
    df = store.read_parquet(store.PRICES_PATH)
    if df is None:
        raise FileNotFoundError("找不到價格資料，請先執行 scripts/update_data.py")
    df["stock_id"] = df["stock_id"].astype(str)
    return df


def load_benchmark() -> pd.DataFrame:
    df = store.read_parquet(store.BENCHMARK_PATH)
    if df is None:
        raise FileNotFoundError("找不到 0050 資料，請先執行 scripts/update_data.py")
    return df


def official_latest_closes() -> pd.DataFrame:
    """TWSE/TPEx OpenAPI 最近一個交易日的官方收盤價。"""
    twse = get_json(TWSE_LATEST_URL)
    tpex = get_json(TPEX_LATEST_URL)
    rows = [(d["Date"], d["Code"], to_num(d["ClosingPrice"])) for d in twse]
    rows += [(d["Date"], d["SecuritiesCompanyCode"], to_num(d["Close"])) for d in tpex]
    df = pd.DataFrame(rows, columns=["roc_date", "stock_id", "close"])
    # 民國年 1151019 -> 2026-10-19
    df["date"] = pd.to_datetime(
        (df["roc_date"].str[:-4].astype(int) + 1911).astype(str) + df["roc_date"].str[-4:], format="%Y%m%d"
    )
    return df[["date", "stock_id", "close"]]


def check_against_official(prices: pd.DataFrame, tolerance: float = 0.001) -> pd.DataFrame:
    """比對 yfinance 與官方收盤價，回傳不一致的列（空表代表全部一致）。"""
    off = official_latest_closes()
    day = off["date"].max()
    ours = prices[prices["date"] == day][["stock_id", "close"]]
    if ours.empty:
        print(f"  抽查略過：價格資料裡沒有 {day.date()} 這天", file=sys.stderr)
        return pd.DataFrame()
    m = ours.merge(off[off["date"] == day], on="stock_id", suffixes=("_yf", "_official"))
    m["diff"] = (m["close_yf"] / m["close_official"] - 1).abs()
    bad = m[m["diff"] > tolerance]
    print(f"  抽查 {day.date()}：比對 {len(m)} 檔，{len(bad)} 檔差異超過 {tolerance:.1%}")
    return bad
