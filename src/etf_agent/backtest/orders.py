"""目標權重 → 整張下單股數。回測與實盤交易書共用。"""
from __future__ import annotations

import math
from typing import Mapping

import pandas as pd

from ..rules import DEFAULT_CAP_BUFFER, RULES, CompetitionRules


def validate_targets(target_w: pd.Series, universe: set[str], rules: CompetitionRules = RULES) -> None:
    bad = sorted(set(target_w.index) - universe)
    if bad:
        raise ValueError(f"目標權重含標的池外的股票：{bad}")
    if (target_w < 0).any():
        raise ValueError(f"目標權重不可為負：{target_w[target_w < 0].to_dict()}")
    if target_w.sum() > 1 + 1e-9:
        raise ValueError(f"目標權重總和 {target_w.sum():.4f} > 1，不允許融資")
    n = int((target_w > 0).sum())
    if not rules.min_holdings <= n <= rules.max_holdings:
        raise ValueError(f"目標持股 {n} 檔，不在 {rules.min_holdings}–{rules.max_holdings} 之間")


def build_orders(
    current: Mapping[str, int],
    target_w: pd.Series,
    exec_prices: pd.Series,
    mark_prices: pd.Series,
    cash: float,
    rules: CompetitionRules = RULES,
    band: float = 0.0,
    cap_buffer: float = DEFAULT_CAP_BUFFER,
) -> dict[str, int]:
    """回傳 {stock_id: 股數變動}，正數買、負數賣，都是整張的倍數。

    exec_prices: 成交價；NaN 代表當天不能交易（停牌等），該檔維持原持股
    mark_prices: 估值用價格（不能交易的股票用它算淨值）
    band: 現有持股與目標權重差距小於 band（占淨值比例）就不動，用來省交易成本
    cap_buffer: 權重上限再往下留一點空間，吸收手續費和價格跳動
    """
    lot = rules.lot_size
    tradable = exec_prices.dropna()
    tradable = tradable[tradable > 0]

    def px(sid: str) -> float:
        p = tradable.get(sid)
        return float(p) if p is not None else float(mark_prices[sid])

    nav = cash + sum(n * px(sid) for sid, n in current.items() if n)
    target_w = target_w[target_w > 0]
    caps = pd.Series({sid: rules.cap_for(sid) - cap_buffer for sid in target_w.index}, dtype=float)
    target_w = target_w.clip(upper=caps)

    names = set(target_w.index) | {sid for sid, n in current.items() if n}
    desired: dict[str, int] = {}
    for sid in names:
        cur = int(current.get(sid, 0))
        if sid not in tradable.index:
            desired[sid] = cur
            continue
        p = float(tradable[sid])
        w = float(target_w.get(sid, 0.0))
        want = math.floor(w * nav / (p * lot)) * lot
        cur_w = cur * p / nav
        within_band = cur > 0 and want > 0 and abs(cur_w - w) < band and cur_w <= rules.cap_for(sid) - cap_buffer
        desired[sid] = cur if within_band else want

    def n_held() -> int:
        return sum(1 for s in names if desired[s] > 0)

    # 賣不掉的部位（停牌）也算檔數；超過上限時放棄權重最小的新買進
    excess = n_held() - rules.max_holdings
    if excess > 0:
        new_buys = sorted((s for s in names if desired[s] > 0 and not current.get(s, 0)),
                          key=lambda s: float(target_w.get(s, 0.0)))
        for s in new_buys[:excess]:
            desired[s] = 0

    def projected_cash() -> float:
        flow = 0.0
        for sid in names:
            d = desired[sid] - int(current.get(sid, 0))
            if d > 0:
                v = d * tradable[sid]
                flow -= v + rules.buy_cost(v)
            elif d < 0:
                v = -d * tradable[sid]
                flow += v - rules.sell_cost(v)
        return cash + flow

    # 現金不夠就從買最多的那檔一張一張減
    while projected_cash() < 0:
        buying = [s for s in names if desired[s] > int(current.get(s, 0))]
        if not buying:
            break
        sid = max(buying, key=lambda s: (desired[s] - int(current.get(s, 0))) * tradable[s])
        desired[sid] -= lot

    # 取整和 band 會讓現金高於目標：多出的現金超過 band 時，一張一張補給低配最多、還沒碰到上限的股票
    target_cash = (1 - float(target_w.sum())) * nav
    if projected_cash() - target_cash > band * nav:
        candidates = [s for s in target_w.index if s in tradable.index]
        while True:
            room = projected_cash() - target_cash
            best, best_gap = None, float("-inf")
            for s in candidates:
                p = float(tradable[s])
                cost = lot * p * (1 + rules.fee_rate)
                if cost > room or (desired[s] + lot) * p / nav > rules.cap_for(s) - cap_buffer:
                    continue
                if desired[s] == 0 and n_held() >= rules.max_holdings:
                    continue
                gap = float(target_w[s]) * nav - desired[s] * p
                # 優先補「加一張後最接近目標」的股票，避免高價股一張就超配太多
                if gap > 0 and gap - 0.5 * lot * p > best_gap:
                    best, best_gap = s, gap - 0.5 * lot * p
            if best is None:
                break
            desired[best] += lot

    deltas = {sid: desired[sid] - int(current.get(sid, 0)) for sid in names}
    return {sid: d for sid, d in deltas.items() if d != 0}
