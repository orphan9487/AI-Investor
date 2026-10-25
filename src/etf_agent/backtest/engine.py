"""逐日回測引擎，照比賽規則模擬：10 億起始、整張成交、手續費與證交稅、每日合規檢查。

時間軸（預設 exec_price="open"）：
  第 t 日決策只能用到 t-1 日收盤（含）以前的資料 → 以 t 日開盤價成交 → 以 t 日收盤價估值
exec_price="close" 時改成以 t 日收盤價成交，決策資料仍然只到 t-1 日。
官方公布成交價基準後，改 BacktestConfig.exec_price 即可。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Protocol

import pandas as pd

from ..data.view import DataView, MarketData
from ..rules import DEFAULT_CAP_BUFFER, RULES, CompetitionRules, check_portfolio
from .metrics import max_drawdown
from .orders import build_orders, validate_targets


class Strategy(Protocol):
    name: str

    def target_weights(self, view: DataView) -> pd.Series: ...


@dataclass
class BacktestConfig:
    exec_price: str = "open"  # "open" 或 "close"
    rebalance_every: int = 1  # 每幾個交易日重新決策一次
    band: float = 0.0  # 見 build_orders
    cap_buffer: float = DEFAULT_CAP_BUFFER
    credit_dividends: bool = True  # 除息日把現金股利記入現金（官方是否計入尚未確認）
    rules: CompetitionRules = field(default_factory=lambda: RULES)


@dataclass
class BacktestResult:
    strategy: str
    config: BacktestConfig
    daily: pd.DataFrame
    trades: pd.DataFrame
    holdings: pd.DataFrame
    violations: pd.DataFrame

    def summary(self) -> dict:
        d = self.daily
        initial = self.config.rules.initial_capital
        return {
            "strategy": self.strategy,
            "start": d.index[0].date().isoformat(),
            "end": d.index[-1].date().isoformat(),
            "days": len(d),
            "final_nav": float(d["nav"].iloc[-1]),
            "total_return": float(d["nav"].iloc[-1] / initial - 1),
            "max_drawdown": max_drawdown(pd.concat([pd.Series([initial]), d["nav"]], ignore_index=True)),
            "fees": float(d["fees"].sum()),
            "taxes": float(d["taxes"].sum()),
            "cost_pct": float((d["fees"].sum() + d["taxes"].sum()) / initial),
            "turnover": float(d["turnover"].sum()),
            "avg_holdings": float(d["n_holdings"].mean()),
            "max_cash_weight": float(d["cash_weight"].max()),
            "violation_days": int(self.violations["date"].nunique()) if not self.violations.empty else 0,
        }


def run_backtest(
    md: MarketData,
    strategy: Strategy,
    start,
    end,
    config: BacktestConfig | None = None,
) -> BacktestResult:
    cfg = config or BacktestConfig()
    rules = cfg.rules
    universe = set(md.stock_ids)

    all_dates = md.dates
    days = all_dates[(all_dates >= pd.Timestamp(start)) & (all_dates <= pd.Timestamp(end))]
    if len(days) == 0:
        raise ValueError(f"{start}–{end} 之間沒有交易日")
    first = all_dates.get_loc(days[0])
    if first == 0:
        raise ValueError("回測第一天之前至少要有一個交易日的資料供決策")

    close_ff = md.panels["close"].ffill()
    exec_panel = md.panels[cfg.exec_price]
    volume = md.panels["volume"]
    events = md.dividend_events if cfg.credit_dividends else None
    if events is not None and not events.empty:
        events = events[events["ex_date"].isin(days)].set_index("ex_date")

    cash = float(rules.initial_capital)
    shares: dict[str, int] = {}
    daily_rows, trade_rows, hold_rows, viol_rows = [], [], [], []

    for i, d in enumerate(days):
        prev = all_dates[first + i - 1]
        fees = taxes = div_cash = traded_value = 0.0

        # 除權息：除息日當天開盤前持有的部位享有股利
        if events is not None and not events.empty and d in events.index:
            for _, ev in events.loc[[d]].iterrows():
                n = shares.get(ev["stock_id"], 0)
                if n <= 0:
                    continue
                div_cash += n * ev["cash"]
                if ev["stock_ratio"] > 0:  # 配股取整張近似；比賽是否處理配股未知
                    shares[ev["stock_id"]] = n + math.floor(n * ev["stock_ratio"] / rules.lot_size) * rules.lot_size
            cash += div_cash

        if i % cfg.rebalance_every == 0:
            view = md.view(prev)
            target = strategy.target_weights(view).astype(float)
            validate_targets(target, universe, rules)
            # 成交量為 0（停牌、面額變更、下市）當天不能交易
            exec_px = exec_panel.loc[d].where(volume.loc[d] > 0)
            mark_px = close_ff.loc[prev]
            orders = build_orders(shares, target, exec_px, mark_px, cash, rules, cfg.band, cfg.cap_buffer)
            for sid, delta in orders.items():
                p = float(exec_px[sid])
                value = abs(delta) * p
                if delta > 0:
                    cost = rules.buy_cost(value)
                    cash -= value + cost
                    fees += cost
                else:
                    fee = value * rules.fee_rate
                    tax = value * rules.sell_tax_rate
                    cash += value - fee - tax
                    fees += fee
                    taxes += tax
                    cost = fee + tax
                traded_value += value
                shares[sid] = shares.get(sid, 0) + delta
                trade_rows.append((d, sid, delta, p, value, cost))
            shares = {s: n for s, n in shares.items() if n != 0}

            post_px = exec_px.fillna(mark_px)
            for prob in check_portfolio(shares, post_px.to_dict(), cash, universe, rules):
                viol_rows.append((d, "post_trade", prob))

        close = close_ff.loc[d]
        stock_value = sum(n * close[s] for s, n in shares.items())
        nav = cash + stock_value
        for prob in check_portfolio(shares, close.to_dict(), cash, universe, rules):
            viol_rows.append((d, "close", prob))

        weights = {s: n * close[s] / nav for s, n in shares.items()}
        hold_rows.extend((d, s, n, weights[s]) for s, n in shares.items())
        daily_rows.append({
            "date": d,
            "nav": nav,
            "cash": cash,
            "stock_value": stock_value,
            "n_holdings": len(shares),
            "cash_weight": cash / nav,
            "max_weight": max(weights.values(), default=0.0),
            "turnover": traded_value / nav,
            "fees": fees,
            "taxes": taxes,
            "dividends": div_cash,
        })

    return BacktestResult(
        strategy=getattr(strategy, "name", type(strategy).__name__),
        config=cfg,
        daily=pd.DataFrame(daily_rows).set_index("date"),
        trades=pd.DataFrame(trade_rows, columns=["date", "stock_id", "shares", "price", "value", "cost"]),
        holdings=pd.DataFrame(hold_rows, columns=["date", "stock_id", "shares", "weight"]),
        violations=pd.DataFrame(viol_rows, columns=["date", "phase", "problem"]),
    )
