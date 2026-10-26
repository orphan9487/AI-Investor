"""最危險的失敗情境：錯誤必須被擋下、原因清楚，且不留下看起來可以提交的檔案。"""
import copy
import json

import pandas as pd
import pytest

from etf_agent.agent import dplan, pipeline
from test_agent import _pack, make_md, sandbox  # noqa: F401（sandbox 是 fixture）
from test_dplan import CLOSE, NAV, PREV, UNIVERSE, cash_for, official_positive


def _formal_run(md, td, pack=None, decision=None, team="TEAM_042"):
    if pack is not None:
        pipeline.INBOX_DIR.mkdir(parents=True, exist_ok=True)
        (pipeline.INBOX_DIR / "dp.json").write_text(json.dumps(pack), encoding="utf-8")
    d = pipeline.prepare(update=False, md=md, trade_date=td, allow_stale=True, holidays=set())
    if decision is not None:
        (d / "decision.json").write_text(decision, encoding="utf-8")
    out = pipeline.finalize(td, md=md, fetch_official=False, cfg=pipeline.AgentConfig(team_id=team))
    return out, json.loads((out / "status.json").read_text(encoding="utf-8"))


def _first_day_pack(td, **kw):
    return _pack(trade_date=td, as_of_date=None, holdings=[],
                 account={"cash": 1e9, "cash_ratio_tradable": 1.0, "nav_tradable": 1e9, "aum": 1e9,
                          "receivable_cash": 0.0, "holdings_count": 0},
                 prev_day_orders={"outcome": "none", "void_reasons": [], "excluded_codes": []}, **kw)


def _td(md):
    return (md.dates[-1] + pd.offsets.BDay(1)).date().isoformat()


def assert_nothing_submittable(out):
    assert not list(out.glob("D-Plan_*.json")), "被擋下時不可以留下官方檔名的 D-Plan"
    assert list(out.glob("DO_NOT_SUBMIT_D-Plan_*.json"))


def test_formal_mode_without_datapack_is_blocked(sandbox):
    md = make_md()
    out, st = _formal_run(md, _td(md))
    assert st["state"] == "blocked" and any("資料包" in p for p in st["problems"])
    assert_nothing_submittable(out)


def test_datapack_for_another_day_is_not_used(sandbox):
    md = make_md()
    out, st = _formal_run(md, _td(md), pack=_first_day_pack("2026-01-05"))
    assert st["state"] == "blocked"
    assert_nothing_submittable(out)


def test_datapack_without_settlement_blocks(sandbox):
    md = make_md()
    td = _td(md)
    pack = _pack(trade_date=td, as_of_date=None, missing=["prev_settlement"], account=None, holdings=None,
                 prev_day_orders={"outcome": "unknown", "void_reasons": [], "excluded_codes": []})
    out, st = _formal_run(md, td, pack=pack)
    assert st["state"] == "blocked" and any("nav_tradable" in p for p in st["problems"])
    assert_nothing_submittable(out)


def test_team_mismatch_blocks(sandbox):
    md = make_md()
    td = _td(md)
    out, st = _formal_run(md, td, pack=_first_day_pack(td), team="TEAM_999")
    assert st["state"] == "blocked" and any("隊伍" in p for p in st["problems"])
    assert_nothing_submittable(out)


def test_valid_first_day_datapack_passes(sandbox):
    md = make_md()
    td = _td(md)
    out, st = _formal_run(md, td, pack=_first_day_pack(td))
    assert st["state"] == "final", st.get("problems")
    assert (out / f"D-Plan_TEAM_042_{td}.json").exists()


def test_malformed_llm_output_falls_back_to_quant(sandbox):
    md = make_md()
    td = _td(md)
    out, st = _formal_run(md, td, pack=_first_day_pack(td), decision="{ 這不是 JSON")
    assert st["state"] == "final" and st["author"] == "fallback"


def test_llm_cannot_inject_unknown_stock_or_huge_tilt(sandbox):
    md = make_md()
    td = _td(md)
    bad = json.dumps({"trade_date": td, "risk_level": "neutral", "author": "claude",
                      "include": [{"stock_id": "9999", "reason": "憑空加入"}],
                      "tilts": [{"stock_id": "2330", "multiplier": 50, "reason": "大量加碼"}]})
    out, st = _formal_run(md, td, pack=_first_day_pack(td), decision=bad)
    assert st["state"] == "final"
    plan = json.loads(next(out.glob("D-Plan_*.json")).read_text(encoding="utf-8"))
    tickers = {d["ticker"] for d in plan["decisions"]}
    assert "9999" not in tickers
    tsmc = next(d for d in plan["decisions"] if d["ticker"] == "2330")
    assert tsmc["target_weight"] <= 0.25


# ---- 驗證器層級 ----

def test_duplicate_orders_are_caught():
    dp = official_positive()
    dp["orders"].append(copy.deepcopy(dp["orders"][1]))
    errors, _ = dplan.validate(dp, PREV, NAV, CLOSE, cash_for(0.05), UNIVERSE)
    assert any("委託重複" in e for e in errors) and any("推導不一致" in e for e in errors)


def test_order_side_must_match_decision_action():
    dp = official_positive()
    dp["orders"][0]["side"] = "BUY"
    errors, _ = dplan.validate(dp, PREV, NAV, CLOSE, cash_for(0.05), UNIVERSE)
    assert any("方向" in e for e in errors)


def test_oversell_is_caught():
    dp = official_positive()
    prev = dict(PREV, **{"2330": 1000})  # 只有 1 張，卻要減到 15 張以下 → 推導出來是買，委託寫賣 2 張
    errors, _ = dplan.validate(dp, prev, NAV, CLOSE, cash_for(0.05), UNIVERSE)
    assert any("賣出超過持股" in e or "推導不一致" in e for e in errors)


def test_negative_cash_is_caught():
    dp = official_positive()
    errors, _ = dplan.validate(dp, PREV, NAV, CLOSE, 0.0, UNIVERSE)
    assert any("現金為負" in e for e in errors)


def test_missing_official_price_is_caught():
    dp = official_positive()
    close = {k: v for k, v in CLOSE.items() if k != "2891"}
    errors, _ = dplan.validate(dp, PREV, NAV, close, cash_for(0.05), UNIVERSE)
    assert any("缺前日收盤價" in e for e in errors)


def test_funding_for_without_real_gap_is_caught():
    dp = official_positive()
    dp["decisions"][0]["funding_for"] = ["D2"]
    # 現金很多：拿掉 2330 的賣出後，現金比仍高於宣告下限 → 缺口不存在
    errors, _ = dplan.validate(dp, PREV, NAV, CLOSE, cash_for(0.09), UNIVERSE)
    assert any("缺口不存在" in e for e in errors)


def test_funding_for_must_point_to_buy():
    dp = official_positive()
    dp["decisions"][1]["funding_for"] = ["D1"]  # D2 是 BUY，不能宣告調度；D1 也不是買進
    errors, _ = dplan.validate(dp, PREV, NAV, CLOSE, cash_for(0.05), UNIVERSE)
    assert any("funding_for" in e for e in errors)


def test_passive_overweight_is_flagged_and_trimmed(sandbox):
    md = make_md()
    td = _td(md)
    shares = {s: 30_000 for s in ["2330"] + [f"{1101 + i}" for i in range(1, 24)]}
    shares["1102"] = 1_100_000                      # 110 萬股 × 100 元 ≈ 淨值 10.6%：被動超限
    holdings = [{"code": s, "shares": n, "receivable_shares": 0, "close_price": 100.0,
                 "tradable_weight": n * 100.0 / 1.04e9} for s, n in shares.items()]
    pack = _pack(trade_date=td, as_of_date=md.dates[-1].date().isoformat(), holdings=holdings,
                 account={"cash": 1.04e9 - sum(shares.values()) * 100.0, "cash_ratio_tradable": 0.2,
                          "nav_tradable": 1.04e9, "aum": 1.04e9, "receivable_cash": 0.0, "holdings_count": 24},
                 compliance={"violations": 0, "notices": 1, "incidents": [
                     {"date": md.dates[-1].date().isoformat(), "check_code": "weight_cap", "status": "warn",
                      "message": "1102 權重超過 10%"}]})
    out, st = _formal_run(md, td, pack=pack)
    health = json.loads((out / "context.json").read_text(encoding="utf-8"))["health"]
    cap = next(c for c in health if c["name"] == "個股權重上限")
    assert not cap["ok"] and "1102" in cap["detail"] and "5 個交易日" in cap["detail"]
    plan = json.loads(next(out.glob("D-Plan_*.json")).read_text(encoding="utf-8"))
    trim = next(o for o in plan["orders"] if o["ticker"] == "1102")
    assert trim["side"] == "SELL"                    # 隔天就減碼，不拖到第 5 天
