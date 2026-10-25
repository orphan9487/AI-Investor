"""量化基準：多因子評分 + 持股遲滯，作為 Agent 每日決策的起點。

因子（各自做橫斷面 z 分數，缺資料的因子自動略過、權重重新分配）：
  mom   近 lookback 日報酬，跳過最近 skip 日（避開短期反轉）
  rev   最近 3 期已公布月營收的平均年增率
  flow  外資 + 投信近 flow_days 日淨買超 / 發行股數

選股：台積電固定持有 tsmc_weight；其他依綜合分數取前 n-1 檔。
遲滯：已持有的股票只要排名還在 keep_rank 以內就續抱，降低換手成本。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..data.view import DataView
from ..rules import RULES
from .baselines import DEFAULT_INVESTED, cap_and_normalize, liquid


def zscore(s: pd.Series, clip: float = 3.0) -> pd.Series:
    s = s.replace([np.inf, -np.inf], np.nan).dropna()
    if len(s) < 3 or s.std() == 0:
        return s * 0.0
    return ((s - s.mean()) / s.std()).clip(-clip, clip)


def revenue_yoy(view: DataView, periods: int = 3) -> pd.Series:
    rev = view.revenue()
    if rev.empty:
        return pd.Series(dtype=float)
    r = rev.set_index(["stock_id", "period"])["revenue"]
    r = r[~r.index.duplicated(keep="last")]
    yoy = r / r.reindex([(s, p - 12) for s, p in r.index]).values - 1
    return yoy.groupby(level="stock_id").apply(lambda x: x.tail(periods).mean())


@dataclass
class CompositeScore:
    n: int = 25
    keep_rank: int = 40
    # 22%：回測中收盤漂移不會超過 25%，績效與 24% 無明顯差異（見 README）
    tsmc_weight: float = 0.22
    invested: float = DEFAULT_INVESTED
    lookback: int = 60
    skip: int = 5
    flow_days: int = 20
    min_avg_value: float = 3e8
    factor_weights: dict = field(default_factory=lambda: {"mom": 0.4, "rev": 0.3, "flow": 0.3})
    name: str = "composite"
    _held: set = field(default_factory=set, repr=False)

    def scores(self, view: DataView) -> pd.DataFrame:
        """每檔可交易、流動性足夠的股票的因子 z 分數與綜合分數，依綜合分數排序。"""
        pool = liquid(view, self.min_avg_value)
        raw = {}
        px = view.panel("adj_close", self.lookback + self.skip + 1)
        raw["mom"] = (px.iloc[-1 - self.skip] / px.iloc[0] - 1).reindex(pool)
        raw["rev"] = revenue_yoy(view).reindex(pool)
        flows = [view.institutional(n, self.flow_days) for n in ("Foreign_Investor", "Investment_Trust")]
        if all(not f.empty for f in flows):
            net = sum(f.sum() for f in flows)
            raw["flow"] = (net / view.shares).reindex(pool)

        df = pd.DataFrame(raw)
        z = {}
        for k, w in self.factor_weights.items():
            if k in df and df[k].notna().sum() >= 10:
                z[k] = zscore(df[k])
        zdf = pd.DataFrame(z).reindex(pool)
        weights = pd.Series({k: self.factor_weights[k] for k in zdf.columns})
        avail = zdf.notna().mul(weights, axis=1).sum(axis=1)
        composite = zdf.fillna(0).mul(weights, axis=1).sum(axis=1) / avail.replace(0, np.nan)

        out = df.add_prefix("raw_").join(zdf.add_prefix("z_"))
        out["score"] = composite
        out = out.dropna(subset=["score"]).sort_values("score", ascending=False)
        out["rank"] = np.arange(1, len(out) + 1)
        return out

    def select(self, view: DataView, held: set[str]) -> tuple[pd.Series, pd.DataFrame]:
        """回傳 (目標權重, 評分表)。held 是目前持股，用來做遲滯。"""
        sc = self.scores(view)
        tsmc = RULES.tsmc_id
        use_tsmc = self.tsmc_weight > 0 and tsmc in view.tradable()
        others = sc.drop(index=tsmc, errors="ignore")
        n_others = self.n - (1 if use_tsmc else 0)

        keep = [s for s in others.index if s in held and others.at[s, "rank"] <= self.keep_rank]
        keep = sorted(keep, key=lambda s: others.at[s, "rank"])[:n_others]
        fill = [s for s in others.index if s not in keep][: n_others - len(keep)]
        chosen = keep + fill

        rest = self.invested - (self.tsmc_weight if use_tsmc else 0.0)
        w = cap_and_normalize(pd.Series(1.0, index=chosen), total=rest)
        if use_tsmc:
            w[tsmc] = self.tsmc_weight
        return w, sc

    def target_weights(self, view: DataView) -> pd.Series:
        w, _ = self.select(view, self._held)
        self._held = set(w.index)
        return w
