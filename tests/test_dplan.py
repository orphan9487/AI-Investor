import copy
import json

import pytest

from etf_agent.agent import dplan

SRC = [{"source_id": f"S{i}", "authority": "twse", "url": "https://openapi.twse.com.tw/v1/x",
        "content_as_of": "2026-10-20T18:30:00+08:00"} for i in (1, 2, 3)]


def official_positive():
    """主辦方正例的結構（數值照抄）。"""
    return {
        "schema_version": "4.3", "doc_type": "D-Plan", "team_id": "TEAM_042", "trade_date": "2026-10-28",
        "sources": copy.deepcopy(SRC),
        "observations": [
            {"obs_id": "O1", "source_ref": ["S1"], "statement": "TAIEX 收 23,412.5（-0.62%）", "values": {"TAIEX": 23412.5}},
            {"obs_id": "O2", "source_ref": ["S2"], "statement": "SOX -1.8%、NDX -0.9%", "values": {"SOX_chg_pct": -1.8}},
            {"obs_id": "O3", "source_ref": ["S3"], "statement": "台指期夜盤 -0.52%", "values": {"gap_vs_day_pct": -0.52}},
        ],
        "market_view": {"basis_refs": ["O1", "O2", "O3"], "logic": "隔夜美股半導體回檔、夜盤小跌，綜合判定中性震盪",
                        "regime": "neutral", "stance": "neutral",
                        "posture": {"net_exposure_intent": "hold", "target_cash_pct_range": [0.03, 0.1]},
                        "counter_evidence": None},
        "inferences": [
            {"inf_id": "I1", "premise_refs": ["O2", "O3"], "logic": "SOX 與 ADR 下跌、夜盤同向 → 調降 2330 曝險",
             "counter_evidence": "基本面未變"},
            {"inf_id": "I2", "premise_refs": ["O1"], "logic": "降息機率上升且金融逆勢 → 新建 2891 部位",
             "counter_evidence": None},
            {"inf_id": "I3", "premise_refs": ["O2"], "logic": "無新事證，權重偏離在無交易帶內，維持不動",
             "counter_evidence": None},
        ],
        "decisions": [
            {"decision_id": "D1", "ticker": "2330", "action": "TRIM", "target_weight": 0.23, "inference_refs": ["I1"]},
            {"decision_id": "D2", "ticker": "2891", "action": "BUY", "target_weight": 0.045, "inference_refs": ["I2"]},
        ],
        "no_trade_decisions": [{"ticker": "2317", "reason_refs": ["I3"]}, {"ticker": "2412", "reason_refs": ["I3"]}],
        "orders": [{"ticker": "2330", "side": "SELL", "shares": 2000, "decision_ref": "D1"},
                   {"ticker": "2891", "side": "BUY", "shares": 108000, "decision_ref": "D2"}],
        "agent_metadata": {"model_provider": "anthropic", "model_version": "claude-opus-4-7",
                           "run_started_at": "2026-10-28T05:02:10+08:00",
                           "run_completed_at": "2026-10-28T07:52:36+08:00", "code_version": "git:a1b2c3d"},
    }


NAV = 102_000_000
CLOSE = {"2330": 1480.0, "2891": 42.5, "2317": 200.0, "2412": 120.0}
PREV = {"2330": 17000, "2317": 20000, "2412": 30000}
UNIVERSE = set(CLOSE)


def cash_for(ratio_after):
    flow = dplan.net_flow(official_positive()["orders"], CLOSE)
    return ratio_after * NAV + flow  # 讓委託後現金比落在宣告區間內


def test_official_positive_example_passes():
    dp = official_positive()
    errors, _ = dplan.validate(dp, PREV, NAV, CLOSE, cash_for(0.05), UNIVERSE)
    # 範例只有 3 檔持股，持股檔數那條必然不過；其餘都要通過
    assert [e for e in errors if "持股" not in e or "檔" not in e] == []


def test_official_negative_example_problems_are_caught():
    dp = official_positive()
    dp["decisions"].append({"decision_id": "D3", "ticker": "2412", "action": "ADD", "target_weight": 0.05,
                            "inference_refs": ["I9"]})                       # 孤兒決策
    dp["no_trade_decisions"] = [n for n in dp["no_trade_decisions"] if n["ticker"] != "2412"]
    dp["orders"][0]["shares"] = 3000                                          # 股數與推導不一致
    dp["market_view"]["posture"]["net_exposure_intent"] = "reduce"            # 宣告與淨流向相反
    errors, _ = dplan.validate(dp, PREV, NAV, CLOSE, cash_for(0.05), UNIVERSE)
    text = "\n".join(errors)
    assert "孤兒決策" in text and "推導不一致" in text and "部位宣告 reduce" in text


def test_missing_coverage_is_an_error():
    dp = official_positive()
    dp["no_trade_decisions"] = dp["no_trade_decisions"][:1]
    errors, _ = dplan.validate(dp, PREV, NAV, CLOSE, cash_for(0.05), UNIVERSE)
    assert any("前日持股 2412 沒有決策" in e for e in errors)


@pytest.mark.parametrize("price", [9.87, 42.5, 155.0, 1480.0, 15900.0])
@pytest.mark.parametrize("lots", [1, 7, 108, 2950])
def test_weight_for_lots_round_trips(price, lots):
    nav = 1_000_000_000
    w = dplan.weight_for_lots(lots * 1000, price, nav)
    assert dplan.target_shares(w, nav, price) == lots * 1000


def test_official_derivation_matches_example_numbers():
    assert dplan.target_shares(0.23, NAV, 1480.0) == 15000
    assert dplan.target_shares(0.045, NAV, 42.5) == 108000
