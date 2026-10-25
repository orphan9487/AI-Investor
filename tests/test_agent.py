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
    monkeypatch.setattr(pipeline, "INBOX_DIR", tmp_path / "inbox")
    monkeypatch.setattr(pipeline, "TEAM_ID_PATH", tmp_path / "state" / "team_id.txt")
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
    d = pipeline.prepare(update=False, md=md1, trade_date=day2.date().isoformat(), allow_stale=True, holidays=set())
    ctx = json.loads((d / "context.json").read_text(encoding="utf-8"))
    assert ctx["data_asof"] == day1.date().isoformat()
    assert len(ctx["base_portfolio"]) == 25
    out = pipeline.finalize(ctx["trade_date"], md=md1, fetch_official=False, cfg=pipeline.AgentConfig(team_id="TEAM_TEST"))
    st = json.loads((out / "status.json").read_text(encoding="utf-8"))
    assert st["state"] == "final" and st["author"] == "fallback"
    orders = pd.read_csv(out / "orders.csv", dtype={"stock_id": str})
    assert (orders["shares"] % 1000 == 0).all() and (orders["shares"] > 0).all()
    assert "合規檢查" in (out / "report.md").read_text(encoding="utf-8")
    plan_file = out / f"D-Plan_TEAM_TEST_{ctx['trade_date']}.json"     # 官方檔名規約
    assert plan_file.exists() and json.loads(plan_file.read_text(encoding="utf-8"))["team_id"] == "TEAM_TEST"
    assert (out / "evidence" / "official_close_prev.csv").exists()

    # 第二天：資料多一天 → 結算前一次委託，帳本有持股
    next_td = (day2 + pd.offsets.BDay(1)).date().isoformat()
    d2 = pipeline.prepare(update=False, md=md, trade_date=next_td, allow_stale=True, holidays=set())
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
    out2 = pipeline.finalize(next_td, md=md, fetch_official=False, cfg=pipeline.AgentConfig(team_id="TEAM_TEST"))
    st2 = json.loads((out2 / "status.json").read_text(encoding="utf-8"))
    assert st2["state"] == "final" and st2["author"] == "claude"
    tgt = pd.read_csv(out2 / "target_holdings.csv", dtype={"stock_id": str})
    assert held[0] not in set(tgt["stock_id"])
    report = (out2 / "report.md").read_text(encoding="utf-8")
    assert "測試剔除理由" in report and "加碼 ×1.3" in report


def test_missing_decision_falls_back():
    d = fallback("2026-10-27", "沒有 decision.json")
    assert d.author == "fallback" and d.risk_level == "neutral" and not d.tilts


def test_expected_last_trading_day():
    from datetime import datetime

    from etf_agent.agent.health import expected_last_trading_day

    hol = {pd.Timestamp("2026-10-26")}
    assert expected_last_trading_day(datetime(2026, 10, 27, 10, 0), hol) == pd.Timestamp("2026-10-23")
    assert expected_last_trading_day(datetime(2026, 10, 27, 18, 0), hol) == pd.Timestamp("2026-10-27")
    assert expected_last_trading_day(datetime(2026, 10, 25, 18, 0), hol) == pd.Timestamp("2026-10-23")


def test_stale_prices_stop_prepare(sandbox):
    from datetime import datetime

    md = make_md()
    late = (md.dates[-1] + pd.Timedelta(days=7)).to_pydatetime().replace(hour=18)
    with pytest.raises(pipeline.StaleDataError, match="價格資料最新"):
        pipeline.prepare(update=False, md=md, trade_date="2026-12-01", now=late, holidays=set())
    st = json.loads((pipeline.run_dir("2026-12-01") / "status.json").read_text(encoding="utf-8"))
    assert st["state"] == "failed"


def test_industry_cap_never_breaks_min_holdings(monkeypatch):
    from etf_agent.strategies.composite import CompositeScore

    md = make_md()
    md.industry = pd.Series("半導體業", index=md.stock_ids)  # 全部同一產業
    w, _ = CompositeScore(max_per_industry=6).select(md.view(md.dates[-1]), set())
    assert len(w) == 25

    md.industry = pd.Series(["半導體業", "航運業"] * 20, index=md.stock_ids)
    w, _ = CompositeScore(max_per_industry=6).select(md.view(md.dates[-1]), set())
    others = w.drop("2330").index
    assert len(w) == 25 and md.industry[others].value_counts().max() >= 6


def test_invalid_day_reverts_like_the_platform():
    from etf_agent.agent.pipeline import day_violations

    led = Ledger(cash=100_000_000, shares={s: 30_000 for s in IDS[1:26]})
    close = pd.Series(100.0, index=IDS)
    before = led.snapshot()
    # 買太多 → 現金變負：整天無效、還原
    res = led.apply_fills(pd.DataFrame({"stock_id": ["1130"], "shares": [2_000_000]}), close,
                          pd.Series(1e7, index=IDS), "2026-10-27")
    assert (res["status"] == "成交").all() and led.cash < 0
    problems = day_violations(led, before, close, set(IDS))
    assert any("現金為負" in p for p in problems)
    led.restore(before)
    assert led.cash == 100_000_000 and "1130" not in led.shares


def test_price_drift_over_cap_is_not_invalid():
    from etf_agent.agent.pipeline import day_violations

    led = Ledger(cash=20_000_000, shares={s: 30_000 for s in IDS[1:26]})
    close = pd.Series(100.0, index=IDS)
    close[IDS[1]] = 500.0  # 沒有加碼、純粹漲價而超過 10%
    assert day_violations(led, led.snapshot(), close, set(IDS)) == []


def test_dividends_accrue_but_do_not_hit_cash():
    led = Ledger(cash=10_000_000, shares={"1101": 10_000})
    ev = pd.DataFrame({"stock_id": ["1101"], "cash": [2.5], "stock_ratio": [0.1]})
    led.accrue_dividends(ev, "2026-11-02")
    assert led.cash == 10_000_000
    assert led.accrued_cash == 25_000 and led.accrued_shares == {"1101": 1000}
    px = pd.Series({"1101": 50.0})
    assert led.final_nav(px) - led.nav(px) == 25_000 + 1000 * 50


def test_target_trade_date_follows_submission_window():
    from datetime import datetime

    hol = {pd.Timestamp("2026-10-26")}
    f = pipeline.target_trade_date
    assert f(datetime(2026, 10, 23, 20, 0), hol) == pd.Timestamp("2026-10-27")  # 週五晚上 → 跳過週末與 10/26
    assert f(datetime(2026, 10, 26, 20, 0), hol) == pd.Timestamp("2026-10-27")  # 休市日晚上
    assert f(datetime(2026, 10, 27, 7, 30), hol) == pd.Timestamp("2026-10-27")  # 早上備援
    assert f(datetime(2026, 10, 27, 9, 30), hol) == pd.Timestamp("2026-10-28")  # 已過截止


# ---- DataPack（依主辦方規格書 3.0 的範例）----

def _pack(**kw):
    base = {"schema_version": "3.0", "doc_type": "DataPack", "team_id": "TEAM_042",
            "trade_date": "2026-10-28", "as_of_date": "2026-10-27", "generated_at": "2026-10-27T18:58:18+08:00",
            "missing": [], "prev_day_orders": {"outcome": "executed", "void_reasons": [], "excluded_codes": []},
            "account": {"cash": 135237741.0, "cash_ratio_tradable": 0.135265, "nav_tradable": 999795141.0,
                        "aum": 1000795141.0, "receivable_cash": 1000000.0, "holdings_count": 2},
            "holdings": [{"code": "2330", "shares": 73000, "receivable_shares": 0, "close_price": 2585.0,
                          "tradable_weight": 0.188744},
                         {"code": "6669", "shares": 0, "receivable_shares": 9913, "close_price": 2610.0,
                          "tradable_weight": None}],
            "compliance": {"violations": 1, "notices": 1, "incidents": [
                {"date": "2026-10-28", "check_code": "oversell", "status": "fail", "message": "賣出超過庫存"}]}}
    base.update(kw)
    return base


def test_datapack_overrides_ledger(tmp_path):
    from etf_agent.agent import datapack as dpk

    f = tmp_path / "dp.json"
    f.write_text(json.dumps(_pack(), ensure_ascii=False), encoding="utf-8")
    dp = dpk.load(f)
    led = Ledger(cash=1.0, shares={"1101": 1000})
    dpk.apply_to_ledger(dp, led)
    assert led.cash == 135237741.0 and led.shares == {"2330": 73000}  # 只有應收配股的不算持股
    assert led.accrued_shares == {"6669": 9913} and led.accrued_cash == 1000000.0
    assert led.asof == "2026-10-27"
    assert any("違規 1 天" in x for x in dpk.summary_lines(dp))


def test_datapack_without_settlement_does_not_touch_ledger(tmp_path):
    from etf_agent.agent import datapack as dpk

    f = tmp_path / "dp.json"
    f.write_text(json.dumps(_pack(as_of_date=None, missing=["prev_settlement"], account=None, holdings=None,
                                  prev_day_orders={"outcome": "unknown", "void_reasons": [], "excluded_codes": []})),
                 encoding="utf-8")
    dp = dpk.load(f)
    led = Ledger(cash=5.0, shares={"1101": 1000})
    assert not dp.has_positions and dpk.apply_to_ledger(dp, led) == {}
    assert led.cash == 5.0 and led.shares == {"1101": 1000}


def test_datapack_sets_trade_date_and_marks_voided_day(sandbox):
    md = make_md()
    inbox = pipeline.INBOX_DIR
    inbox.mkdir(parents=True)
    pack = _pack(trade_date="2026-12-02", as_of_date="2026-12-01",
                 prev_day_orders={"outcome": "voided", "void_reasons": ["insufficient_funds"], "excluded_codes": []},
                 account={"cash": 1e9, "cash_ratio_tradable": 1.0, "nav_tradable": 1e9, "aum": 1e9,
                          "receivable_cash": 0.0, "holdings_count": 0}, holdings=[])
    (inbox / "datapack.json").write_text(json.dumps(pack), encoding="utf-8")
    d = pipeline.prepare(update=False, md=md, trade_date="2026-12-02", allow_stale=True, holidays=set())
    ctx = json.loads((d / "context.json").read_text(encoding="utf-8"))
    assert ctx["trade_date"] == "2026-12-02" and ctx["platform"]["prev_outcome"] == "voided"
    st = json.loads((pipeline.run_dir("2026-12-01") / "status.json").read_text(encoding="utf-8"))
    assert st["state"] == "invalid" and st["problems"] == ["insufficient_funds"]
    assert "整批作廢" in (d / "context.md").read_text(encoding="utf-8")


def test_submission_window():
    from datetime import datetime

    hol = {pd.Timestamp("2026-10-26")}
    w = pipeline.submission_window
    assert w(datetime(2026, 10, 26, 20, 30), hol)[0] == "2026-10-27"   # 休市日晚上，明天交易
    assert w(datetime(2026, 10, 25, 20, 30), hol)[0] is None           # 週日晚上，但 10/26 休市
    assert w(datetime(2026, 11, 1, 20, 30), hol)[0] == "2026-11-02"    # 一般週日晚上 → 週一
    assert w(datetime(2026, 10, 30, 20, 30), hol)[0] is None           # 週五晚上
    assert w(datetime(2026, 10, 27, 6, 30), hol)[0] == "2026-10-27"    # 交易日早上
    assert w(datetime(2026, 10, 31, 6, 30), hol)[0] is None            # 週六早上
    assert w(datetime(2026, 10, 27, 12, 0), hol)[0] is None            # 盤中


def test_unknown_team_blocks_finalize(sandbox):
    md = make_md()
    td = (md.dates[-1] + pd.offsets.BDay(1)).date().isoformat()
    pipeline.prepare(update=False, md=md, trade_date=td, allow_stale=True, holidays=set())
    out = pipeline.finalize(td, md=md, fetch_official=False, cfg=pipeline.AgentConfig(team_id=""))
    st = json.loads((out / "status.json").read_text(encoding="utf-8"))
    assert st["state"] == "blocked" and any("team_id" in p for p in st["problems"])


def test_datapack_accepts_schema_4(tmp_path):
    from etf_agent.agent import datapack as dpk

    f = tmp_path / "dp.json"
    f.write_text(json.dumps(_pack(schema_version="4.3")), encoding="utf-8")
    assert dpk.load(f).team_id == "TEAM_042"
    f.write_text(json.dumps(_pack(schema_version="9.0")), encoding="utf-8")
    with pytest.raises(dpk.DataPackError):
        dpk.load(f)


def test_rerun_archives_previous_version(sandbox):
    md = make_md()
    td = (md.dates[-1] + pd.offsets.BDay(1)).date().isoformat()
    cfg = pipeline.AgentConfig(team_id="TEAM_TEST")
    pipeline.prepare(update=False, md=md, trade_date=td, allow_stale=True, holidays=set())
    pipeline.finalize(td, md=md, fetch_official=False, cfg=cfg)
    pipeline.prepare(update=False, md=md, trade_date=td, allow_stale=True, holidays=set())   # 晨間重跑
    snaps = list((pipeline.run_dir(td) / "previous").glob("*_run"))
    assert len(snaps) == 1
    assert (snaps[0] / "context.json").exists() and (snaps[0] / "evidence").is_dir()
    assert (snaps[0] / f"D-Plan_TEAM_TEST_{td}.json").exists()


def test_bad_datapack_reason_is_reported(sandbox):
    md = make_md()
    pipeline.INBOX_DIR.mkdir(parents=True)
    (pipeline.INBOX_DIR / "dp.json").write_text(json.dumps(_pack(schema_version="9.9")), encoding="utf-8")
    d = pipeline.prepare(update=False, md=md, trade_date="2026-12-02", allow_stale=True, holidays=set())
    health = json.loads((d / "context.json").read_text(encoding="utf-8"))["health"]
    assert any("dp.json" in c["detail"] and "9.9" in c["detail"] for c in health)
