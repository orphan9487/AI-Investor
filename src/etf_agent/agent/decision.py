"""LLM 決策的格式、驗證與套用。

Claude 每天產出 decision.json，只能在以下範圍內調整量化基準組合：
  risk_level  現金水位（aggressive / neutral / cautious / defensive）
  tilts       個股加減碼倍數 0.5–1.5，最多 8 檔
  exclude     從基準組合剔除，最多 5 檔（台積電不能剔除，只能減碼）
  include     從候選名單（排名 <= CANDIDATE_RANK）加入，最多 3 檔
每一項都要寫理由。超出範圍的項目會被修正或丟棄並記錄警告，不會讓整份決策失效；
完全讀不到 decision.json 時退回量化基準（risk_level = neutral、不做任何調整）。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

import pandas as pd

from ..backtest.orders import validate_targets
from ..rules import DEFAULT_CAP_BUFFER, RULES, CompetitionRules
from ..strategies.baselines import cap_and_normalize

RISK_LEVELS = {"aggressive": 0.985, "neutral": 0.98, "cautious": 0.93, "defensive": 0.88}
TILT_RANGE = (0.5, 1.5)
MAX_TILTS = 8
MAX_EXCLUDE = 5
MAX_INCLUDE = 3
CANDIDATE_RANK = 40
MIN_REASON_LEN = 4


@dataclass
class Action:
    stock_id: str
    reason: str
    multiplier: float | None = None
    sources: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)  # 引用 decision.json observations[].id
    counter: str | None = None  # 反方理由，寫進 D-Plan 推論的 counter_evidence


@dataclass
class Decision:
    trade_date: str
    risk_level: str = "neutral"
    market_summary: str = ""
    events: list[dict] = field(default_factory=list)
    tilts: list[Action] = field(default_factory=list)
    exclude: list[Action] = field(default_factory=list)
    include: list[Action] = field(default_factory=list)
    notes: str = ""
    author: str = "fallback"

    def to_dict(self) -> dict:
        return asdict(self)


def fallback(trade_date: str, why: str) -> Decision:
    return Decision(trade_date=trade_date, author="fallback", notes=f"未採用 LLM 決策：{why}")


def parse_decision(obj: dict, trade_date: str, base: pd.Series, candidates: pd.DataFrame,
                   rules: CompetitionRules = RULES) -> tuple[Decision, list[str]]:
    """把 LLM 的 JSON 轉成 Decision，回傳 (decision, 警告清單)。"""
    warn: list[str] = []
    if obj.get("trade_date") and obj["trade_date"] != trade_date:
        warn.append(f"trade_date {obj['trade_date']} 與本次 {trade_date} 不符，已改為 {trade_date}")

    risk = obj.get("risk_level", "neutral")
    if risk not in RISK_LEVELS:
        warn.append(f"risk_level {risk!r} 無效，改用 neutral")
        risk = "neutral"

    eligible = set(candidates.index[candidates["rank"] <= CANDIDATE_RANK]) | set(base.index)

    def actions(key: str, limit: int, allowed: set[str], need_mult: bool) -> list[Action]:
        out, seen = [], set()
        for item in obj.get(key) or []:
            sid = str(item.get("stock_id", "")).strip()
            reason = str(item.get("reason", "")).strip()
            if sid in seen:
                warn.append(f"{key}: {sid} 重複，只取第一筆")
                continue
            if sid not in allowed:
                warn.append(f"{key}: {sid} 不在允許範圍，略過")
                continue
            if len(reason) < MIN_REASON_LEN:
                warn.append(f"{key}: {sid} 沒有寫理由，略過")
                continue
            mult = None
            if need_mult:
                try:
                    mult = float(item.get("multiplier"))
                except (TypeError, ValueError):
                    warn.append(f"{key}: {sid} multiplier 無效，略過")
                    continue
                lo, hi = TILT_RANGE
                if not lo <= mult <= hi:
                    warn.append(f"{key}: {sid} multiplier {mult} 超出 {lo}–{hi}，已修正")
                    mult = min(max(mult, lo), hi)
            if len(out) >= limit:
                warn.append(f"{key}: 超過 {limit} 筆上限，{sid} 以後略過")
                break
            seen.add(sid)
            out.append(Action(sid, reason, mult, [str(s) for s in item.get("sources") or []],
                              [str(e) for e in item.get("evidence") or []],
                              str(item["counter"]) if item.get("counter") else None))
        return out

    exclude = actions("exclude", MAX_EXCLUDE, set(base.index) - {rules.tsmc_id}, False)
    if any(str(i.get("stock_id")) == rules.tsmc_id for i in obj.get("exclude") or []):
        warn.append("台積電不能剔除，只能用 tilts 減碼")
    excluded = {a.stock_id for a in exclude}
    include = actions("include", MAX_INCLUDE, eligible - set(base.index), False)
    tilts = actions("tilts", MAX_TILTS, (set(base.index) | {a.stock_id for a in include}) - excluded, True)

    d = Decision(
        trade_date=trade_date,
        risk_level=risk,
        market_summary=str(obj.get("market_summary", "")),
        events=list(obj.get("events") or []),
        tilts=tilts,
        exclude=exclude,
        include=include,
        notes=str(obj.get("notes", "")),
        author=str(obj.get("author", "claude")),
    )
    return d, warn


def apply_decision(d: Decision, base: pd.Series, candidates: pd.DataFrame,
                   rules: CompetitionRules = RULES) -> tuple[pd.Series, pd.DataFrame]:
    """回傳 (最終目標權重, 調整明細)。"""
    tsmc = rules.tsmc_id
    excluded = {a.stock_id for a in d.exclude}
    names = [s for s in base.index if s not in excluded] + [a.stock_id for a in d.include]

    # 剔除後檔數不足：依排名從候選名單補
    target_n = max(len(base), rules.min_holdings)
    auto_fill = []
    for s in candidates.index:
        if len(names) >= target_n:
            break
        if s not in names and s not in excluded:
            names.append(s)
            auto_fill.append(s)
    names = names[: rules.max_holdings]

    others_base = base.drop(index=tsmc, errors="ignore")
    default_w = float(others_base.mean()) if len(others_base) else 0.04
    mult = {a.stock_id: a.multiplier for a in d.tilts}
    raw = pd.Series({s: float(base.get(s, default_w)) * mult.get(s, 1.0) for s in names})

    invested = RISK_LEVELS[d.risk_level]
    tsmc_w = 0.0
    if tsmc in raw.index:
        # 台積電維持基準權重 × 倍數，現金水位調整由其他股票等比例吸收
        tsmc_w = min(float(raw[tsmc]), rules.tsmc_max_weight - DEFAULT_CAP_BUFFER)
        raw = raw.drop(index=tsmc)
    final = cap_and_normalize(raw, total=invested - tsmc_w, rules=rules)
    if tsmc_w > 0:
        final[tsmc] = tsmc_w
    final = final.sort_values(ascending=False)
    validate_targets(final, set(candidates.index) | set(base.index), rules)

    reasons = {a.stock_id: ("剔除", a.reason) for a in d.exclude}
    reasons |= {a.stock_id: ("加入", a.reason) for a in d.include}
    reasons |= {s: ("自動補位", "剔除後依量化排名遞補") for s in auto_fill}
    for a in d.tilts:
        kind = "加碼" if a.multiplier > 1 else "減碼" if a.multiplier < 1 else "維持"
        reasons[a.stock_id] = (f"{kind} ×{a.multiplier:g}", a.reason)
    rows = []
    for s in sorted(set(base.index) | set(final.index), key=lambda s: -final.get(s, 0)):
        action, reason = reasons.get(s, ("", ""))
        rows.append({"stock_id": s, "base_weight": float(base.get(s, 0.0)), "final_weight": float(final.get(s, 0.0)),
                     "action": action, "reason": reason})
    return final, pd.DataFrame(rows)
