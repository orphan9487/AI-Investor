"""TWSE / TPEx 官方單日全市場行情。一次請求拿到一個市場當天所有股票。"""
from __future__ import annotations

import time

import pandas as pd
import requests

TWSE_DAILY_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
TPEX_DAILY_URL = "https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes"
TWSE_MIN_INTERVAL = 3.0  # TWSE 對短時間大量請求會暫時封鎖 IP

_last_twse = 0.0
QUOTE_COLUMNS = ["stock_id", "name", "open", "high", "low", "close", "volume", "value", "vwap"]


def get_json(url: str, params: dict | None = None, retries: int = 3):
    """連線偶爾中斷（大檔案 IncompleteRead），重試幾次再放棄。"""
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=60)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError):
            if attempt == retries - 1:
                raise
            time.sleep(5 * (attempt + 1))


def to_num(s) -> float:
    try:
        return float(str(s).replace(",", "").strip())
    except ValueError:
        return float("nan")


_mi_index_cache: dict[str, dict] = {}


def _twse_mi_index(date) -> dict:
    global _last_twse
    key = pd.Timestamp(date).strftime("%Y%m%d")
    if key in _mi_index_cache:
        return _mi_index_cache[key]
    wait = TWSE_MIN_INTERVAL - (time.monotonic() - _last_twse)
    if wait > 0:
        time.sleep(wait)
    _last_twse = time.monotonic()
    j = get_json(TWSE_DAILY_URL, {"date": key, "type": "ALLBUT0999", "response": "json"})
    if j.get("stat") == "OK":
        _mi_index_cache[key] = j
    return j


def twse_taiex(date) -> dict | None:
    """加權指數收盤與漲跌幅（%）；休市日回傳 None。"""
    j = _twse_mi_index(date)
    if j.get("stat") != "OK":
        return None
    for t in j.get("tables", []):
        for row in t.get("data") or []:
            if str(row[0]).strip() == "發行量加權股價指數":
                sign = -1.0 if ("-" in str(row[2]) or "green" in str(row[2])) else 1.0
                return {"close": to_num(row[1]), "chg_pct": sign * abs(to_num(row[4]))}
    return None


def twse_daily_quotes(date) -> pd.DataFrame:
    """欄位：stock_id, name, open, high, low, close, volume, value（成交金額）, vwap（成交均價）。休市日回傳空表。"""
    j = _twse_mi_index(date)
    if j.get("stat") != "OK":
        return pd.DataFrame(columns=QUOTE_COLUMNS)
    table = next(t for t in j["tables"] if t.get("fields") and "收盤價" in t["fields"])
    f = table["fields"]
    col = lambda name, conv: [conv(row[f.index(name)]) for row in table["data"]]
    df = pd.DataFrame({
        "stock_id": col("證券代號", str.strip),
        "name": col("證券名稱", str.strip),
        "open": col("開盤價", to_num),
        "high": col("最高價", to_num),
        "low": col("最低價", to_num),
        "close": col("收盤價", to_num),
        "volume": col("成交股數", to_num),
        "value": col("成交金額", to_num),
    })
    df["vwap"] = df["value"] / df["volume"].where(df["volume"] > 0)
    return df


def tpex_daily_quotes(date) -> pd.DataFrame:
    """欄位：同 twse_daily_quotes，另加 shares（發行股數）。"""
    ts = pd.Timestamp(date)
    j = get_json(TPEX_DAILY_URL, {"date": ts.strftime("%Y/%m/%d"), "response": "json"})
    tables = j.get("tables") or []
    # 日期不符代表該日沒有資料（伺服器會改回傳最近一個交易日）
    if j.get("date") != ts.strftime("%Y%m%d") or not tables or not tables[0].get("data"):
        return pd.DataFrame(columns=QUOTE_COLUMNS + ["shares"])
    table = tables[0]
    f = table["fields"]
    col = lambda name, conv: [conv(row[f.index(name)]) for row in table["data"]]
    return pd.DataFrame({
        "stock_id": col("代號", str.strip),
        "name": col("名稱", str.strip),
        "open": col("開盤", to_num),
        "high": col("最高", to_num),
        "low": col("最低", to_num),
        "close": col("收盤", to_num),
        "volume": col("成交股數", to_num),
        "value": col("成交金額(元)", to_num),
        "vwap": col("均價", to_num),
        "shares": col("發行股數", to_num),
    })


# ---- 法人買賣超（輸出成 FinMind TaiwanStockInstitutionalInvestorsBuySell 的長表格式）----

TWSE_INSTI_URL = "https://www.twse.com.tw/rwd/zh/fund/T86"
TPEX_INSTI_URL = "https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade"

# (FinMind 名稱, 買進欄位索引, 賣出欄位索引)
_TWSE_INSTI_COLS = [
    ("Foreign_Investor", 2, 3),
    ("Foreign_Dealer_Self", 5, 6),
    ("Investment_Trust", 8, 9),
    ("Dealer_self", 12, 13),
    ("Dealer_Hedging", 15, 16),
]
# TPEx 欄位：代號、名稱、外資(不含自營) 買賣淨、外資自營 買賣淨、外資合計 買賣淨、投信 買賣淨、
# 自營(自行) 買賣淨、自營(避險) 買賣淨、自營合計 買賣淨、三大法人合計
_TPEX_INSTI_COLS = [
    ("Foreign_Investor", 2, 3),
    ("Foreign_Dealer_Self", 5, 6),
    ("Investment_Trust", 11, 12),
    ("Dealer_self", 14, 15),
    ("Dealer_Hedging", 17, 18),
]
INSTI_COLUMNS = ["date", "stock_id", "name", "buy", "sell"]


def _insti_rows(rows, cols, ts) -> pd.DataFrame:
    out = []
    for row in rows:
        sid = row[0].strip()
        for name, b, s in cols:
            out.append((ts, sid, name, to_num(row[b]), to_num(row[s])))
    return pd.DataFrame(out, columns=INSTI_COLUMNS)


def twse_institutional(date) -> pd.DataFrame:
    global _last_twse
    wait = TWSE_MIN_INTERVAL - (time.monotonic() - _last_twse)
    if wait > 0:
        time.sleep(wait)
    _last_twse = time.monotonic()
    ts = pd.Timestamp(date)
    j = get_json(TWSE_INSTI_URL, {"date": ts.strftime("%Y%m%d"), "selectType": "ALL", "response": "json"})
    if j.get("stat") != "OK":
        return pd.DataFrame(columns=INSTI_COLUMNS)
    return _insti_rows(j["data"], _TWSE_INSTI_COLS, ts)


def tpex_institutional(date) -> pd.DataFrame:
    ts = pd.Timestamp(date)
    j = get_json(TPEX_INSTI_URL, {"date": ts.strftime("%Y/%m/%d"), "type": "Daily", "sect": "EW", "response": "json"})
    tables = j.get("tables") or []
    if j.get("date") != ts.strftime("%Y%m%d") or not tables or not tables[0].get("data"):
        return pd.DataFrame(columns=INSTI_COLUMNS)
    return _insti_rows(tables[0]["data"], _TPEX_INSTI_COLS, ts)


# ---- 月營收（最新一期全市場；輸出成 FinMind TaiwanStockMonthRevenue 格式）----

TWSE_REVENUE_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap05_L"
TPEX_REVENUE_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O"


def latest_monthly_revenue() -> pd.DataFrame:
    rows = []
    for url in (TWSE_REVENUE_URL, TPEX_REVENUE_URL):
        for d in get_json(url):
            ym = str(d["資料年月"]).strip()  # 民國年月，例 11509
            year, month = int(ym[:-2]) + 1911, int(ym[-2:])
            # FinMind 的 date 是營收月份的次月 1 日；OpenAPI 單位是千元
            avail = pd.Timestamp(year=year, month=month, day=1) + pd.offsets.MonthBegin(1)
            rows.append((avail, d["公司代號"].strip(), "Taiwan", to_num(d["營業收入-當月營收"]) * 1000, month, year, ""))
    return pd.DataFrame(rows, columns=["date", "stock_id", "country", "revenue", "revenue_month", "revenue_year",
                                       "create_time"])


# ---- 休市日 ----

TWSE_HOLIDAY_URL = "https://openapi.twse.com.tw/v1/holidaySchedule/holidaySchedule"


def twse_holidays() -> set[pd.Timestamp]:
    """TWSE 公布的當年度休市日（含只辦理交割、不交易的日子）。"""
    out = set()
    for d in get_json(TWSE_HOLIDAY_URL):
        roc = str(d["Date"]).strip()
        out.add(pd.Timestamp(f"{int(roc[:-4]) + 1911}-{roc[-4:-2]}-{roc[-2:]}"))
    return out


def next_trading_day(after, holidays: set[pd.Timestamp] | None = None) -> pd.Timestamp:
    holidays = twse_holidays() if holidays is None else holidays
    d = pd.Timestamp(after) + pd.Timedelta(days=1)
    while d.weekday() >= 5 or d in holidays:
        d += pd.Timedelta(days=1)
    return d


# ---- 重大訊息（公開資訊觀測站；端點只提供最近一個交易日，需每天累積）----

TWSE_ANNOUNCE_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap04_L"
TPEX_ANNOUNCE_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap04_O"
ANNOUNCE_COLUMNS = ["date", "time", "stock_id", "name", "subject", "clause", "fact_date", "detail"]


def _roc_date(s) -> pd.Timestamp | None:
    s = str(s).strip()
    if len(s) < 6 or not s.isdigit():
        return None
    return pd.Timestamp(f"{int(s[:-4]) + 1911}-{s[-4:-2]}-{s[-2:]}")


def latest_announcements() -> pd.DataFrame:
    rows = []
    for url, sid_key, name_key in ((TWSE_ANNOUNCE_URL, "公司代號", "公司名稱"),
                                   (TPEX_ANNOUNCE_URL, "SecuritiesCompanyCode", "CompanyName")):
        for d in get_json(url):
            # TWSE 的「主旨」欄位名稱尾端有空白
            subject = next((v for k, v in d.items() if k.strip() == "主旨"), "")
            rows.append((
                _roc_date(d.get("發言日期")), str(d.get("發言時間", "")).strip(), str(d[sid_key]).strip(),
                str(d[name_key]).strip(), " ".join(str(subject).split()), str(d.get("符合條款", "")).strip(),
                _roc_date(d.get("事實發生日")), " ".join(str(d.get("說明", "")).split())[:800],
            ))
    return pd.DataFrame(rows, columns=ANNOUNCE_COLUMNS)
