from __future__ import annotations

import pandas as pd


def max_drawdown(nav: pd.Series) -> float:
    """回傳負數，例如 -0.08 代表最大回撤 8%。"""
    return float((nav / nav.cummax() - 1).min())


def benchmark_return(bench: pd.DataFrame, start, end, exec_price: str = "open") -> float:
    """0050 在同一段期間的含息報酬，進場價對齊回測的成交價基準。

    bench: 長表 date, open, close, adj_close（單一標的）
    """
    b = bench.set_index("date").sort_index()
    b = b.loc[pd.Timestamp(start): pd.Timestamp(end)]
    if b.empty:
        return float("nan")
    first = b.iloc[0]
    adj = first["adj_close"] / first["close"]
    entry = first[exec_price] * adj
    return float(b["adj_close"].iloc[-1] / entry - 1)
