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
