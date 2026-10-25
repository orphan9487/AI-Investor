"""基準策略。之後真正的 Agent 策略要能穩定贏過這幾個，才有意義。

每個策略只實作 target_weights(view) -> Series(stock_id -> 權重)，
權重總和 <= 1，剩下的就是現金。
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from ..data.view import DataView
from ..rules import DEFAULT_CAP_BUFFER, RULES, CompetitionRules

DEFAULT_INVESTED = 0.98  # 留 2% 現金付手續費、吸收整張取整


def cap_and_normalize(
    raw: pd.Series,
    total: float = DEFAULT_INVESTED,
    rules: CompetitionRules = RULES,
    cap_buffer: float = DEFAULT_CAP_BUFFER,
) -> pd.Series:
    """依 raw 比例分配 total，超過個股上限的部分按比例分給其他檔（water-filling）。"""
    raw = raw[raw > 0].astype(float)
    if raw.empty:
        return raw
    caps = pd.Series({s: rules.cap_for(s) - cap_buffer for s in raw.index})
    w = pd.Series(0.0, index=raw.index)
    free = raw.index
    remaining = total
    while len(free) and remaining > 1e-12:
        alloc = raw[free] / raw[free].sum() * remaining
        over = alloc > caps[free] - w[free]
        if not over.any():
            w[free] += alloc
            break
        hit = alloc.index[over]
        remaining -= (caps[hit] - w[hit]).sum()
        w[hit] = caps[hit]
        free = free.difference(hit)
    return w


def liquid(view: DataView, min_avg_value: float, lookback: int = 20) -> pd.Index:
    """近 lookback 日平均成交金額 >= min_avg_value 的股票。單一部位可能到 1 億，流動性要夠。"""
    value = (view.panel("close", lookback) * view.panel("volume", lookback)).mean()
    return value.index[value >= min_avg_value].intersection(view.tradable())


@dataclass
class CapWeighted:
    """市值加權前 n 大、套用權重上限。最接近「大盤」的做法。"""

    n: int = 30
    name: str = "cap_weighted"

    def target_weights(self, view: DataView) -> pd.Series:
        mcap = view.market_cap().dropna().nlargest(self.n)
        return cap_and_normalize(mcap)


@dataclass
class EqualWeight:
    n: int = 25
    name: str = "equal_weight"

    def target_weights(self, view: DataView) -> pd.Series:
        top = view.market_cap().dropna().nlargest(self.n)
        return cap_and_normalize(pd.Series(1.0, index=top.index))


@dataclass
class Momentum:
    """過去 lookback 日報酬（跳過最近 skip 日）最強的 n 檔，等權重。"""

    n: int = 25
    lookback: int = 60
    skip: int = 5
    min_avg_value: float = 3e8
    name: str = "momentum"

    def target_weights(self, view: DataView) -> pd.Series:
        px = view.panel("adj_close", self.lookback + self.skip + 1)
        ret = px.iloc[-1 - self.skip] / px.iloc[0] - 1
        ret = ret.reindex(liquid(view, self.min_avg_value)).dropna()
        top = ret.nlargest(self.n)
        return cap_and_normalize(pd.Series(1.0, index=top.index))


@dataclass
class RevenueGrowth:
    """最新一期已公布月營收年增率最高的 n 檔，等權重。需要先下載 FinMind 月營收。"""

    n: int = 25
    min_avg_value: float = 3e8
    name: str = "revenue_growth"

    def target_weights(self, view: DataView) -> pd.Series:
        rev = view.revenue()
        if rev.empty:
            raise RuntimeError("沒有月營收資料，請先執行 scripts/update_data.py --finmind revenue")
        rev = rev.set_index(["stock_id", "period"])["revenue"]
        latest = rev.groupby(level="stock_id").tail(1)
        prior = latest.index.map(lambda k: (k[0], k[1] - 12))
        yoy = pd.Series(latest.values / rev.reindex(prior).values - 1, index=latest.index.get_level_values(0))
        yoy = yoy.replace([float("inf"), -float("inf")], float("nan")).dropna()
        yoy = yoy.reindex(liquid(view, self.min_avg_value)).dropna()
        top = yoy.nlargest(self.n)
        return cap_and_normalize(pd.Series(1.0, index=top.index))

