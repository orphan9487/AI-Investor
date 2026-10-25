"""組合集中度與相關性診斷：避免「以為分散，其實同一注下了很多次」。"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..data.view import DataView


def concentration(view: DataView, weights: pd.Series, lookback: int = 60, high_corr: float = 0.7) -> dict:
    w = weights[weights > 0]
    w = w / w.sum()
    rets = view.panel("adj_close", lookback + 1)[w.index].pct_change().iloc[1:]
    rets = rets.dropna(axis=1, how="all").fillna(0.0)
    w = w.reindex(rets.columns).fillna(0.0)
    w = w / w.sum()

    cov = rets.cov().values * 252
    vols = np.sqrt(np.diag(cov))
    port_vol = float(np.sqrt(w.values @ cov @ w.values))
    # 分散比 = 個股波動加權平均 / 組合波動；平方約等於「相互獨立的部位數」
    dr = float(w.values @ vols / port_vol) if port_vol > 0 else float("nan")

    corr = rets.corr()
    iu = np.triu_indices(len(corr), k=1)
    pairs = pd.DataFrame({"a": corr.index[iu[0]], "b": corr.columns[iu[1]], "corr": corr.values[iu]})
    avg_corr = float(pairs["corr"].mean()) if len(pairs) else float("nan")
    top = pairs[pairs["corr"] >= high_corr].sort_values("corr", ascending=False).head(10)

    ind = view.industry.reindex(w.index).fillna("未分類")
    by_ind = (pd.DataFrame({"industry": ind, "weight": w})
              .groupby("industry")["weight"].agg(["sum", "count"]).sort_values("sum", ascending=False))
    return {
        "annual_vol": port_vol,
        "diversification_ratio": dr,
        "effective_bets": dr ** 2,
        "n_holdings": int((w > 0).sum()),
        "avg_pair_corr": avg_corr,
        "high_corr_pairs": [{"a": r.a, "b": r.b, "corr": round(float(r.corr), 3)} for r in top.itertuples()],
        "industry": [{"industry": i, "weight": round(float(r["sum"]), 4), "count": int(r["count"])}
                     for i, r in by_ind.iterrows()],
    }


def render_concentration_md(c: dict, names: dict[str, str] | None = None) -> str:
    names = names or {}
    lines = [
        f"- 預估年化波動 {c['annual_vol']:.1%}；平均兩兩相關係數 {c['avg_pair_corr']:.2f}",
        f"- {c['n_holdings']} 檔持股約等於 **{c['effective_bets']:.1f} 個獨立部位**（分散比 {c['diversification_ratio']:.2f}）",
        "",
        "| 產業 | 權重 | 檔數 |",
        "|---|---|---|",
        *[f"| {r['industry']} | {r['weight']:.1%} | {r['count']} |" for r in c["industry"]],
    ]
    if c["high_corr_pairs"]:
        lines += ["", "高度相關（60 日報酬相關係數 >= 0.7）："]
        lines += [f"- {p['a']} {names.get(p['a'], '')} ↔ {p['b']} {names.get(p['b'], '')}：{p['corr']:.2f}"
                  for p in c["high_corr_pairs"]]
    return "\n".join(lines)
