import json

import numpy as np
import pandas as pd
import pytest

from etf_agent.agent import pipeline
from etf_agent.agent.decision import apply_decision, parse_decision, fallback
from etf_agent.agent.ledger import Ledger
from etf_agent.data.view import MarketData
from etf_agent.rules import RULES

IDS = ["2330"] + [f"{1101 + i}" for i in range(39)]


def make_md(n_days=90, seed=0):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2026-06-01", periods=n_days)
    rows = []
    for j, sid in enumerate(IDS):
        drift = 0.002 * (j % 7 - 3) / 3
        p = 100 * np.cumprod(1 + drift + 0.01 * rng.standard_normal(n_days))
        for t, d in enumerate(dates):
            rows.append((d, sid, p[t], p[t], p[t], p[t], p[t], 1e7))
    prices = pd.DataFrame(rows, columns=["date", "stock_id", "open", "high", "low", "close", "adj_close", "volume"])
    uni = pd.DataFrame({"stock_id": IDS, "market": "twse", "shares": [2.6e10] + [1e9] * 39,
                        "name": [f"股{s}" for s in IDS]})
    return MarketData(prices=prices, universe=uni)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(pipeline, "LEDGER_PATH", tmp_path / "state" / "ledger.json")
    return tmp_path


def base_and_candidates():
    base = pd.Series({"2330": 0.24, **{s: 0.74 / 24 for s in IDS[1:25]}})
    cand = pd.DataFrame({"rank": range(1, len(IDS) + 1)}, index=["2330"] + IDS[1:])
    return base, cand


def test_parse_clamps_and_drops_bad_items():
    base, cand = base_and_candidates()
    obj = {
        "risk_level": "yolo",
        "tilts": [{"stock_id": "1102", "multiplier": 3, "reason": "營收創新高"},
                  {"stock_id": "9999", "multiplier": 1.2, "reason": "不存在"},
                  {"stock_id": "1103", "multiplier": 1.2, "reason": ""}],
        "exclude": [{"stock_id": "2330", "reason": "不喜歡"}, {"stock_id": "1104", "reason": "利空新聞"}],
        "include": [{"stock_id": "1130", "reason": "法說會上修"}],
    }
    d, warn = parse_decision(obj, "2026-10-27", base, cand)
    assert d.risk_level == "neutral"
    assert [(a.stock_id, a.multiplier) for a in d.tilts] == [("1102", 1.5)]
    assert [a.stock_id for a in d.exclude] == ["1104"]
    assert [a.stock_id for a in d.include] == ["1130"]
    assert any("台積電不能剔除" in w for w in warn)
    assert any("9999" in w for w in warn) and any("沒有寫理由" in w for w in warn)


def test_apply_keeps_rules_and_backfills():
    base, cand = base_and_candidates()
    obj = {"risk_level": "defensive",
           "exclude": [{"stock_id": s, "reason": "測試剔除"} for s in IDS[1:6]],
           "tilts": [{"stock_id": "2330", "multiplier": 0.5, "reason": "降低集中度"}]}
    d, _ = parse_decision(obj, "2026-10-27", base, cand)
    final, adj = apply_decision(d, base, cand)
    assert len(final) == len(base)  # 剔除 5 檔、自動補 5 檔
    assert not set(IDS[1:6]) & set(final.index)
    assert final["2330"] == pytest.approx(0.12)
    assert final.sum() == pytest.approx(0.88)
    assert final.drop("2330").max() <= RULES.stock_max_weight
    assert (adj["action"] == "自動補位").sum() == 5


def test_ledger_fills_sells_first_and_skips_untradable():
    led = Ledger(cash=1_000_000, shares={"1101": 10_000})
    orders = pd.DataFrame({"stock_id": ["1102", "1101", "1103"], "shares": [15_000, -10_000, 1000]})
    px = pd.Series({"1101": 100.0, "1102": 70.0, "1103": 50.0})
    vol = pd.Series({"1101": 1e6, "1102": 1e6, "1103": 0.0})
    res = led.apply_fills(orders, px, vol, "2026-10-27")
    assert res.set_index("stock_id").loc["1103", "status"].startswith("未成交")
    expected = 1_000_000 + 1_000_000 * (1 - RULES.fee_rate - RULES.sell_tax_rate) - 1_050_000 * (1 + RULES.fee_rate)
    assert led.cash == pytest.approx(expected)
    assert led.shares == {"1102": 15_000}


def test_full_daily_cycle(sandbox):
    md = make_md()
    day1, day2 = md.dates[-2], md.dates[-1]
    md1 = MarketData(prices=md.prices[md.prices["date"] <= day1], universe=md.universe)

    # 第一天：沒有 decision.json → 退回量化基準
    d = pipeline.prepare(update=False, md=md1, trade_date=day2.date().isoformat())
    ctx = json.loads((d / "context.json").read_text(encoding="utf-8"))
    assert ctx["data_asof"] == day1.date().isoformat()
    assert len(ctx["base_portfolio"]) == 25
    out = pipeline.finalize(ctx["trade_date"], md=md1)
    st = json.loads((out / "status.json").read_text(encoding="utf-8"))
    assert st["state"] == "final" and st["author"] == "fallback"
    orders = pd.read_csv(out / "orders.csv", dtype={"stock_id": str})
    assert (orders["shares"] % 1000 == 0).all() and (orders["shares"] > 0).all()
    assert "合規檢查" in (out / "report.md").read_text(encoding="utf-8")

    # 第二天：資料多一天 → 結算前一次委託，帳本有持股
    next_td = (day2 + pd.offsets.BDay(1)).date().isoformat()
    d2 = pipeline.prepare(update=False, md=md, trade_date=next_td)
    led = pipeline.load_ledger()
    assert len(led.shares) == 25 and 0 < led.cash < 0.05 * RULES.initial_capital
    assert json.loads((out / "status.json").read_text(encoding="utf-8"))["state"] == "settled"

    # Claude 的決策：剔除一檔、加碼一檔
    ctx2 = json.loads((d2 / "context.json").read_text(encoding="utf-8"))
    held = [r["stock_id"] for r in ctx2["base_portfolio"] if r["stock_id"] != "2330"]
    (d2 / "decision.json").write_text(json.dumps({
        "trade_date": next_td, "risk_level": "neutral", "market_summary": "測試",
        "exclude": [{"stock_id": held[0], "reason": "測試剔除理由"}],
        "tilts": [{"stock_id": held[1], "multiplier": 1.3, "reason": "測試加碼理由"}],
        "author": "claude",
    }, ensure_ascii=False), encoding="utf-8")
    out2 = pipeline.finalize(next_td, md=md)
    st2 = json.loads((out2 / "status.json").read_text(encoding="utf-8"))
    assert st2["state"] == "final" and st2["author"] == "claude"
    tgt = pd.read_csv(out2 / "target_holdings.csv", dtype={"stock_id": str})
    assert held[0] not in set(tgt["stock_id"])
    report = (out2 / "report.md").read_text(encoding="utf-8")
    assert "測試剔除理由" in report and "加碼 ×1.3" in report


def test_missing_decision_falls_back():
    d = fallback("2026-10-27", "沒有 decision.json")
    assert d.author == "fallback" and d.risk_level == "neutral" and not d.tilts
