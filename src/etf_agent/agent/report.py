"""每日「Agent 決策報告」。格式待主辦單位公布範本後調整。"""
from __future__ import annotations

from datetime import datetime

import pandas as pd

from ..rules import RULES
from .decision import RISK_LEVELS, Decision

RISK_LABEL = {"aggressive": "積極", "neutral": "中性", "cautious": "謹慎", "defensive": "防禦"}


def _pct(x: float) -> str:
    return f"{x:.2%}"


def _md_table(df: pd.DataFrame) -> str:
    if df.empty:
        return "（無）"
    cols = list(df.columns)
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for _, r in df.iterrows():
        out.append("| " + " | ".join("" if pd.isna(v) else str(v) for v in r.values) + " |")
    return "\n".join(out)


def render_report(ctx: dict, d: Decision, warnings: list[str], adjustments: pd.DataFrame,
                  orders: pd.DataFrame, target: pd.DataFrame, cash: float, nav: float,
                  problems: list[str], cfg) -> str:
    p = ctx["portfolio"]
    source = "Claude（LLM 研究員）" if d.author != "fallback" else "量化基準（未採用 LLM 決策）"
    buys = orders[orders["shares"] > 0]
    sells = orders[orders["shares"] < 0]
    turnover = orders["est_value"].sum() / nav if nav else 0.0
    est_cost = (buys["est_value"].sum() * RULES.fee_rate
                + sells["est_value"].sum() * (RULES.fee_rate + RULES.sell_tax_rate))

    lines = [
        f"# Agent 決策報告｜交易日 {ctx['trade_date']}",
        "",
        f"- 資料基準：{ctx['data_asof']} 收盤",
        f"- 產出時間：{datetime.now().isoformat(timespec='seconds')}",
        f"- 決策來源：{source}",
        f"- 成交價假設：{cfg.exec_price}（以 {ctx['data_asof']} 收盤價估算股數）",
        "",
        "## 一、市場觀點",
        "",
        f"**風險水位：{RISK_LABEL[d.risk_level]}**（目標投入 {_pct(RISK_LEVELS[d.risk_level])}）",
        "",
        d.market_summary or "（無）",
    ]
    if d.events:
        lines += ["", "近期重要事件："]
        lines += [f"- {e.get('date', '')} {e.get('event', '')}：{e.get('impact', '')}" for e in d.events]

    lines += [
        "",
        "## 二、投資組合概況",
        "",
        "| 項目 | 交易前 | 交易後（估） |",
        "|---|---|---|",
        f"| 淨值 | {p['nav']:,.0f} | {nav:,.0f} |",
        f"| 累積報酬 | {_pct(p['return_since_start'])} | {_pct(nav / RULES.initial_capital - 1)} |",
        f"| 現金比重 | {_pct(p['cash_weight'])} | {_pct(cash / nav)} |",
        f"| 持股檔數 | {p['n_holdings']} | {len(target)} |",
        "",
        f"本次交易金額約占淨值 {_pct(turnover)}，預估手續費與證交稅 {est_cost:,.0f} 元。",
        "",
        "## 三、Agent 調整與理由",
        "",
    ]
    adj = adjustments[adjustments["action"] != ""].copy()
    if adj.empty:
        lines.append("今日未對量化基準組合做任何調整。")
    else:
        adj["基準權重"] = adj["base_weight"].map(_pct)
        adj["最終權重"] = adj["final_weight"].map(_pct)
        lines.append(_md_table(adj[["stock_id", "name", "action", "基準權重", "最終權重", "reason"]]
                               .rename(columns={"stock_id": "代號", "name": "名稱", "action": "調整", "reason": "理由"})))
    sources = sorted({s for a in d.tilts + d.exclude + d.include for s in a.sources})
    if sources:
        lines += ["", "參考資料：", *[f"- {s}" for s in sources]]

    lines += ["", "## 四、交易明細", ""]
    if orders.empty:
        lines.append("今日無交易。")
    else:
        o = orders.copy()
        o["參考價"] = o["ref_price"].map(lambda v: f"{v:,.2f}")
        o["預估金額"] = o["est_value"].map(lambda v: f"{v:,.0f}")
        lines.append(_md_table(o[["stock_id", "name", "side", "lots", "參考價", "預估金額"]]
                               .rename(columns={"stock_id": "代號", "name": "名稱", "side": "買賣", "lots": "張數"})))

    t = target.copy()
    t["權重"] = t["weight"].map(_pct)
    lines += ["", "## 五、交易後目標持股", "",
              _md_table(t[["stock_id", "name", "lots", "權重"]].rename(
                  columns={"stock_id": "代號", "name": "名稱", "lots": "張數"}))]

    max_other = t.loc[t["stock_id"] != RULES.tsmc_id, "weight"].max() if len(t) else 0.0
    tsmc = t.loc[t["stock_id"] == RULES.tsmc_id, "weight"].sum()
    checks = [
        (f"持股 {RULES.min_holdings}–{RULES.max_holdings} 檔", RULES.min_holdings <= len(t) <= RULES.max_holdings,
         f"{len(t)} 檔"),
        (f"台積電 <= {RULES.tsmc_max_weight:.0%}", tsmc <= RULES.tsmc_max_weight, _pct(tsmc)),
        (f"其他個股 <= {RULES.stock_max_weight:.0%}", max_other <= RULES.stock_max_weight, _pct(max_other)),
        (f"現金 < {RULES.max_cash_weight:.0%}", cash / nav < RULES.max_cash_weight, _pct(cash / nav)),
        ("現金 >= 0（不融資）", cash >= 0, f"{cash:,.0f}"),
        ("只買賣整張", bool((orders["shares"] % RULES.lot_size == 0).all()) if len(orders) else True, ""),
    ]
    lines += ["", "## 六、合規檢查", "", "| 規則 | 結果 | 數值 |", "|---|---|---|"]
    lines += [f"| {name} | {'✅' if ok else '❌'} | {val} |" for name, ok, val in checks]
    if problems:
        lines += ["", "**⚠ 合規檢查未通過，本日交易書不應提交：**", *[f"- {x}" for x in problems]]

    base = pd.DataFrame(ctx["base_portfolio"]).head(10)
    if not base.empty:
        base["基準權重"] = base["base_weight"].map(_pct)
        base["動能"] = base["mom_raw"].map(lambda v: "" if v is None or pd.isna(v) else _pct(v))
        base["營收年增(3M)"] = base["rev_yoy_3m"].map(lambda v: "" if v is None or pd.isna(v) else _pct(v))
        lines += ["", "## 七、量化訊號（基準組合前 10 檔）", "",
                  "因子：動能（60 日報酬，跳過最近 5 日）、近 3 個月營收年增率、外資與投信 20 日淨買超。", "",
                  _md_table(base[["stock_id", "name", "rank", "score", "基準權重", "動能", "營收年增(3M)"]]
                            .rename(columns={"stock_id": "代號", "name": "名稱", "rank": "排名", "score": "分數"}))]

    if warnings or d.notes:
        lines += ["", "## 八、附註與警告", ""]
        lines += [f"- {w}" for w in warnings]
        if d.notes and d.notes not in warnings:
            lines.append(f"- {d.notes}")
    return "\n".join(lines) + "\n"
