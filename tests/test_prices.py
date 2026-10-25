import pandas as pd

from etf_agent.data import prices as P


def test_repair_replaces_placeholder_and_missing_days(monkeypatch):
    dates = pd.bdate_range("2026-03-02", periods=4)
    rows = []
    for sid, m in (("2330", "twse"), ("2317", "twse"), ("5274", "tpex"), ("6488", "tpex")):
        for i, d in enumerate(dates):
            if d == dates[2] and m == "twse":
                continue  # 上市整天缺漏
            placeholder = d == dates[1] and m == "tpex"  # 上櫃佔位：量 0、價不動
            rows.append((d, sid, 100.0, 100.0, 100.0, 100.0, 50.0 if i < 2 else 100.0, 0.0 if placeholder else 1e6))
    df = pd.DataFrame(rows, columns=["date", "stock_id", "open", "high", "low", "close", "adj_close", "volume"])
    markets = {"2330": "twse", "2317": "twse", "5274": "tpex", "6488": "tpex"}

    def quotes(sids):
        return lambda d: pd.DataFrame({"stock_id": sids, "name": "", "open": 101.0, "high": 102.0,
                                       "low": 99.0, "close": 101.0, "volume": 5e5})
    monkeypatch.setattr(P, "twse_daily_quotes", quotes(["2330", "2317"]))
    monkeypatch.setattr(P, "tpex_daily_quotes", quotes(["5274", "6488"]))

    out = P.repair_bad_days(df, dates, markets)
    assert len(out) == 16
    day1 = out[(out["date"] == dates[1]) & (out["stock_id"] == "5274")].iloc[0]
    assert day1["volume"] == 5e5 and day1["close"] == 101.0
    assert day1["adj_close"] == 101.0  # 用下一個交易日的還原比例（1.0）
    assert out[out["date"] == dates[2]]["stock_id"].nunique() == 4


def test_repair_converts_raw_exchange_prices_to_adjusted_basis(monkeypatch):
    dates = pd.bdate_range("2026-03-02", periods=3)
    rows = [(d, "2327", 150.0, 150.0, 150.0, 150.0, 150.0, 1e6 if d != dates[1] else 0.0, 150.0) for d in dates]
    df = pd.DataFrame(rows, columns=P.COLUMNS)
    # 之後有一次 1 拆 4：yfinance 歷史價已除以 4，交易所當天原始價是 600
    splits = pd.DataFrame({"date": [pd.Timestamp("2026-08-14")], "stock_id": ["2327"], "ratio": [4.0]})
    monkeypatch.setattr(P, "twse_daily_quotes", lambda d: pd.DataFrame(
        {"stock_id": ["2327", "2330"], "name": "", "open": 600.0, "high": 604.0, "low": 596.0, "close": 600.0,
         "volume": 2.5e5, "value": 1.5e8, "vwap": 600.0}))
    out = P.repair_bad_days(df, dates, {"2327": "twse"}, splits)
    day = out[out["date"] == dates[1]].iloc[0]
    assert day["close"] == 150.0 and day["vwap"] == 150.0 and day["volume"] == 1e6
