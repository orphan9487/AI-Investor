"""時間點正確（point-in-time）的資料存取。

策略只能拿到 DataView，DataView 只會回傳 asof 當天（含）以前「已經公開」的資料，
從結構上避免回測偷看未來。

- 價格、法人買賣超：當天收盤後公布，asof 當天可用
- 月營收：法規要求次月 10 日前公布；FinMind 的 date 是次月 1 日，
  這裡保守地當作次月 11 日才可用
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from functools import cached_property

import pandas as pd

REVENUE_AVAILABLE_LAG = pd.Timedelta(days=10)
PAR_VALUE = 10.0  # 股票股利換算配股比例用


@dataclass
class MarketData:
    """一次載入、整理好的寬表資料，供回測反覆切片。"""

    prices: pd.DataFrame  # 長表 date, stock_id, open, high, low, close, adj_close, volume
    universe: pd.DataFrame  # stock_id, market, shares, ...
    revenue: pd.DataFrame = field(default_factory=pd.DataFrame)  # FinMind TaiwanStockMonthRevenue
    institutional: pd.DataFrame = field(default_factory=pd.DataFrame)  # FinMind 法人買賣超
    dividend: pd.DataFrame = field(default_factory=pd.DataFrame)  # FinMind TaiwanStockDividend

    def __post_init__(self):
        self.stock_ids: list[str] = sorted(self.universe["stock_id"].astype(str))
        p = self.prices[self.prices["stock_id"].isin(self.stock_ids)]
        self.panels: dict[str, pd.DataFrame] = {
            f: p.pivot(index="date", columns="stock_id", values=f).sort_index().reindex(columns=self.stock_ids)
            for f in ("open", "high", "low", "close", "adj_close", "volume")
        }
        coverage = self.panels["close"].notna().sum(axis=1)
        thin = coverage.index[coverage < 0.5 * len(self.stock_ids)]
        if len(thin):
            warnings.warn(f"移除資料不完整的日期（不到一半股票有價格）：{[d.date() for d in thin]}，"
                          "請重跑 scripts/update_data.py 補齊", stacklevel=2)
            self.panels = {f: df.drop(index=thin) for f, df in self.panels.items()}
        self.dates: pd.DatetimeIndex = self.panels["close"].index
        self.shares: pd.Series = (
            self.universe.set_index("stock_id")["shares"].astype(float).reindex(self.stock_ids)
            if "shares" in self.universe.columns
            else pd.Series(float("nan"), index=self.stock_ids)
        )

    @classmethod
    def load(cls, with_finmind: bool = True) -> "MarketData":
        from .finmind import load_cached
        from .prices import load_prices
        from .universe import load_universe

        uni = load_universe()
        ids = uni["stock_id"].astype(str).tolist()
        kw = {}
        if with_finmind:
            kw = {k: load_cached(k, ids) for k in ("revenue", "institutional", "dividend")}
        return cls(prices=load_prices(), universe=uni, **kw)

    @cached_property
    def revenue_pit(self) -> pd.DataFrame:
        if self.revenue.empty:
            return self.revenue
        r = self.revenue.copy()
        r["available_date"] = pd.to_datetime(r["date"]) + REVENUE_AVAILABLE_LAG
        r["period"] = pd.PeriodIndex.from_fields(year=r["revenue_year"], month=r["revenue_month"], freq="M")
        return r.sort_values(["stock_id", "period"])

    @cached_property
    def institutional_net(self) -> dict[str, pd.DataFrame]:
        """{法人類別: 寬表(date × stock_id) 淨買超股數}"""
        if self.institutional.empty:
            return {}
        df = self.institutional.copy()
        df["net"] = df["buy"] - df["sell"]
        out = {}
        for name, g in df.groupby("name"):
            out[name] = g.pivot_table(index="date", columns="stock_id", values="net", aggfunc="sum") \
                .reindex(columns=self.stock_ids).sort_index()
        return out

    @cached_property
    def dividend_events(self) -> pd.DataFrame:
        """除權息事件：ex_date, stock_id, cash（每股現金股利）, stock_ratio（每股配股數）"""
        cols = ["ex_date", "stock_id", "cash", "stock_ratio"]
        if self.dividend.empty:
            return pd.DataFrame(columns=cols)
        d = self.dividend
        num = lambda c: pd.to_numeric(d.get(c, 0), errors="coerce").fillna(0.0)
        cash = pd.DataFrame({
            "ex_date": pd.to_datetime(d["CashExDividendTradingDate"], errors="coerce"),
            "stock_id": d["stock_id"],
            "cash": num("CashEarningsDistribution") + num("CashStatutorySurplus"),
            "stock_ratio": 0.0,
        })
        stock = pd.DataFrame({
            "ex_date": pd.to_datetime(d["StockExDividendTradingDate"], errors="coerce"),
            "stock_id": d["stock_id"],
            "cash": 0.0,
            "stock_ratio": (num("StockEarningsDistribution") + num("StockStatutorySurplus")) / PAR_VALUE,
        })
        ev = pd.concat([cash[cash["cash"] > 0], stock[stock["stock_ratio"] > 0]], ignore_index=True)
        ev = ev.dropna(subset=["ex_date"])
        return ev.groupby(["ex_date", "stock_id"], as_index=False)[["cash", "stock_ratio"]].sum()

    def view(self, asof) -> "DataView":
        return DataView(self, pd.Timestamp(asof))


class DataView:
    def __init__(self, md: MarketData, asof: pd.Timestamp):
        self._md = md
        self.asof = asof

    @property
    def stock_ids(self) -> list[str]:
        return self._md.stock_ids

    @property
    def shares(self) -> pd.Series:
        return self._md.shares

    def panel(self, field: str = "close", lookback: int | None = None) -> pd.DataFrame:
        """date × stock_id 寬表，只含 asof（含）以前的交易日。lookback 取最後 N 個交易日。"""
        df = self._md.panels[field].loc[: self.asof]
        return df.iloc[-lookback:] if lookback else df

    def last(self, field: str = "close") -> pd.Series:
        return self.panel(field).ffill().iloc[-1]

    def tradable(self) -> pd.Index:
        """asof 當天有成交的股票。停牌或已下市的不能買，策略應只從這裡挑。"""
        vol = self.panel("volume").iloc[-1]
        return vol.index[vol > 0]

    def market_cap(self) -> pd.Series:
        """發行股數 × 最新收盤價（發行股數固定取標的池檔案裡的值），只含 tradable()"""
        return (self.shares * self.last("close")).reindex(self.tradable())

    def revenue(self) -> pd.DataFrame:
        r = self._md.revenue_pit
        if r.empty:
            return r
        return r[r["available_date"] <= self.asof]

    def institutional(self, name: str, lookback: int | None = None) -> pd.DataFrame:
        """name 例：Foreign_Investor、Investment_Trust、Dealer_self、Dealer_Hedging"""
        df = self._md.institutional_net.get(name)
        if df is None:
            return pd.DataFrame(columns=self.stock_ids)
        df = df.loc[: self.asof]
        return df.iloc[-lookback:] if lookback else df
