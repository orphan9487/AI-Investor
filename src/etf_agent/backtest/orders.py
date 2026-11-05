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
    force: frozenset[str] | set[str] = frozenset(),
) -> dict[str, int]:
    """回傳 {stock_id: 股數變動}，正數買、負數賣，都是整張的倍數。

    exec_prices: 成交價；NaN 代表當天不能交易（停牌等），該檔維持原持股
    mark_prices: 估值用價格（不能交易的股票用它算淨值）
    band: 現有持股與目標權重差距小於 band（占淨值比例）就不動，用來省交易成本；
          但現金比目標少超過 2 個 band 時（例如風險水位調降），會解除 band 減碼
    cap_buffer: 權重上限再往下留一點空間，吸收手續費和價格跳動
    force: 不套用 band 的股票，例如 Agent 指定加減碼的個股（倍數造成的權重變化常小於 band）
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
    banded: dict[str, int] = {}  # 因 band 維持原股數的股票 → 目標股數
    for sid in names:
        cur = int(current.get(sid, 0))
        if sid not in tradable.index:
            desired[sid] = cur
            continue
        p = float(tradable[sid])
        w = float(target_w.get(sid, 0.0))
        want = math.floor(w * nav / (p * lot)) * lot
        cur_w = cur * p / nav
        within_band = (sid not in force and cur > 0 and want > 0 and abs(cur_w - w) < band
                       and cur_w <= rules.cap_for(sid) - cap_buffer)
        desired[sid] = cur if within_band else want
        if within_band and want != cur:
            banded[sid] = want

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

    # band 會讓股票部位高於目標：風險水位從 98% 降到 88% 時，各檔只差零點幾個百分點，一檔都不會減。
    # 下單前的現金比目標少超過 2 個 band 時，從超配最多的股票開始解除 band、回到目標股數，直到回到目標。
    # 門檻用下單前現金、而且是 2 個 band：維持原風險水位時，漲多造成的小幅超配照舊不動（回測驗證不變）
    target_cash = (1 - float(target_w.sum())) * nav
    if cash < target_cash - 2 * band * nav:
        over = sorted((s for s in banded if banded[s] < desired[s]),
                      key=lambda s: -(desired[s] - banded[s]) * float(tradable[s]))
        for sid in over:
            if projected_cash() >= target_cash:
                break
            desired[sid] = banded[sid]

    # 現金不夠就從買最多的那檔一張一張減
    while projected_cash() < 0:
        buying = [s for s in names if desired[s] > int(current.get(s, 0))]
        if not buying:
            break
        sid = max(buying, key=lambda s: (desired[s] - int(current.get(s, 0))) * tradable[s])
        desired[sid] -= lot

    # 取整和 band 會讓現金高於目標：多出的現金超過 band 時，一張一張補給低配最多、還沒碰到上限的股票
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
