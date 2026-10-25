"""滾動視窗評估：把歷史切成許多段「24 個交易日」，每段都從 10 億現金開始跑一次。

比賽只看 24 天的最終市值，一次長回測的年化報酬沒有參考價值；
要看的是「在隨機一段 24 天裡，策略贏 0050 的機率與幅度」。
"""
from __future__ import annotations

from typing import Callable

import pandas as pd

from ..data.view import MarketData
from .engine import BacktestConfig, Strategy, run_backtest
from .metrics import benchmark_return


def rolling_windows(
    md: MarketData,
    make_strategy: Callable[[], Strategy],
    bench: pd.DataFrame | None = None,
    window: int = 24,
    step: int = 5,
    start=None,
    end=None,
    config: BacktestConfig | None = None,
    min_history: int = 120,
) -> pd.DataFrame:
    """回傳每個視窗一列的 DataFrame。

    min_history: 第一個視窗之前至少保留多少交易日，給需要歷史資料的因子用
    """
    cfg = config or BacktestConfig()
    dates = md.dates
    lo = max(min_history, dates.searchsorted(pd.Timestamp(start)) if start else 0)
    hi = dates.searchsorted(pd.Timestamp(end), side="right") if end else len(dates)

    rows = []
    for s in range(lo, hi - window + 1, step):
        d0, d1 = dates[s], dates[s + window - 1]
        res = run_backtest(md, make_strategy(), d0, d1, cfg)
        row = res.summary()
        if bench is not None:
            row["bench_return"] = benchmark_return(bench, d0, d1, cfg.exec_price)
            row["excess"] = row["total_return"] - row["bench_return"]
        rows.append(row)
    return pd.DataFrame(rows)


def describe_windows(df: pd.DataFrame) -> pd.Series:
    out = {
        "windows": len(df),
        "mean_return": df["total_return"].mean(),
        "median_return": df["total_return"].median(),
        "p10_return": df["total_return"].quantile(0.10),
        "p90_return": df["total_return"].quantile(0.90),
        "worst_drawdown": df["max_drawdown"].min(),
        "mean_cost_pct": df["cost_pct"].mean(),
        "violation_windows": int((df["violation_days"] > 0).sum()),
    }
    if "excess" in df:
        out |= {
            "mean_excess": df["excess"].mean(),
            "median_excess": df["excess"].median(),
            "beat_bench_rate": (df["excess"] > 0).mean(),
        }
    return pd.Series(out)
