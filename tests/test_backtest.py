import numpy as np
import pandas as pd
import pytest

from etf_agent.backtest.engine import BacktestConfig, run_backtest
from etf_agent.backtest.orders import build_orders, validate_targets
from etf_agent.data.view import MarketData
from etf_agent.rules import DEFAULT_CAP_BUFFER, RULES, check_portfolio
from etf_agent.strategies.baselines import CapWeighted, EqualWeight, cap_and_normalize

IDS = ["2330"] + [f"{1101 + i}" for i in range(29)]


def make_md(n_days=10, price=100.0, drift=None, dividend=None):
    dates = pd.bdate_range("2026-01-05", periods=n_days)
    rows = []
    for j, sid in enumerate(IDS):
        for t, d in enumerate(dates):
            p = price * (1 + (drift or {}).get(sid, 0.0)) ** t
            rows.append((d, sid, p, p, p, p, p, 1e7))
    prices = pd.DataFrame(rows, columns=["date", "stock_id", "open", "high", "low", "close", "adj_close", "volume"])
    uni = pd.DataFrame({"stock_id": IDS, "market": "twse", "shares": [1e10] + [1e9] * 29})
    return MarketData(prices=prices, universe=uni, dividend=dividend if dividend is not None else pd.DataFrame())


class Fixed:
    name = "fixed"

    def __init__(self, w):
        self.w = w

    def target_weights(self, view):
        return self.w


def test_check_portfolio_flags_each_rule():
    prices = {s: 100.0 for s in IDS}
    ok = {s: 30_000 for s in IDS[:25]}  # 25 檔各 300 萬
    nav_cash = 10_000_000
    assert check_portfolio(ok, prices, nav_cash) == []

    probs = check_portfolio({**ok, IDS[1]: 31_500}, prices, nav_cash)
    assert any("不是整張" in p for p in probs)

    few = {s: 30_000 for s in IDS[:19]}
    assert any("持股檔數 19" in p for p in check_portfolio(few, prices, 1_000_000))

    heavy = {**ok, IDS[1]: 200_000}
    assert any(f"{IDS[1]} 權重" in p for p in check_portfolio(heavy, prices, nav_cash))

    assert any("現金比重" in p for p in check_portfolio(ok, prices, 100_000_000))
    assert any("現金為負" in p for p in check_portfolio(ok, prices, -1))


def test_tsmc_has_higher_cap():
    w = cap_and_normalize(pd.Series({"2330": 10.0, **{s: 1.0 for s in IDS[1:21]}}))
    assert w["2330"] == pytest.approx(0.25 - DEFAULT_CAP_BUFFER)
    assert (w.drop("2330") <= 0.10 - DEFAULT_CAP_BUFFER + 1e-12).all()
    assert w.sum() == pytest.approx(0.98)


def test_build_orders_whole_lots_and_cash():
    px = pd.Series(2400.0, index=IDS)
    target = cap_and_normalize(pd.Series(1.0, index=IDS[:25]))
    orders = build_orders({}, target, px, px, RULES.initial_capital)
    assert all(d % 1000 == 0 and d > 0 for d in orders.values())
    spent = sum(d * 2400.0 for d in orders.values())
    assert spent * (1 + RULES.fee_rate) <= RULES.initial_capital


def test_build_orders_shrinks_buys_when_cash_short():
    px = pd.Series(1000.0, index=IDS)
    target = pd.Series(0.04, index=IDS[:25])  # 合計 100%，再加手續費一定不夠
    orders = build_orders({}, target, px, px, 100_000_000)
    cost = sum(d * 1000.0 for d in orders.values())
    assert cost * (1 + RULES.fee_rate) <= 100_000_000


def test_band_avoids_small_trades():
    px = pd.Series(100.0, index=IDS)
    current = {s: 390_000 for s in IDS[:25]}  # 每檔 3900 萬
    cash = 1_000_000_000 - 25 * 39_000_000
    target = pd.Series(0.0391, index=IDS[:25])
    assert build_orders(current, target, px, px, cash, band=0.005) == {}
    assert build_orders(current, target, px, px, cash, band=0.0) != {}


def test_validate_targets():
    with pytest.raises(ValueError, match="標的池外"):
        validate_targets(pd.Series(0.04, index=IDS[:24] + ["9999"]), set(IDS))
    with pytest.raises(ValueError, match="不在 20–30"):
        validate_targets(pd.Series(0.05, index=IDS[:10]), set(IDS))


def test_flat_prices_lose_exactly_the_buy_fee():
    md = make_md()
    res = run_backtest(md, EqualWeight(n=25), md.dates[1], md.dates[-1], BacktestConfig(band=0.01))
    traded = res.trades["value"].sum()
    assert res.trades["shares"].gt(0).all()  # 價格不動 + band → 只有第一天買進
    assert res.daily["nav"].iloc[-1] == pytest.approx(RULES.initial_capital - traded * RULES.fee_rate)
    assert res.violations.empty


def test_sell_charges_fee_and_tax():
    md = make_md(n_days=4)
    a, b = IDS[:25], IDS[5:30]

    class Switch:
        name = "switch"

        def __init__(self):
            self.calls = 0

        def target_weights(self, view):
            self.calls += 1
            return cap_and_normalize(pd.Series(1.0, index=a if self.calls == 1 else b))

    res = run_backtest(md, Switch(), md.dates[1], md.dates[2])
    sold = res.trades.loc[res.trades["shares"] < 0, "value"].sum()
    bought = res.trades.loc[res.trades["shares"] > 0, "value"].sum()
    expected = RULES.initial_capital - bought * RULES.fee_rate - sold * (RULES.fee_rate + RULES.sell_tax_rate)
    assert res.daily["nav"].iloc[-1] == pytest.approx(expected)
    assert res.daily["taxes"].sum() == pytest.approx(sold * RULES.sell_tax_rate)


def test_decision_uses_only_previous_day():
    md = make_md(n_days=5)
    seen = []

    class Spy:
        name = "spy"

        def target_weights(self, view):
            seen.append((view.asof, view.panel("close").index.max()))
            return cap_and_normalize(pd.Series(1.0, index=IDS[:25]))

    run_backtest(md, Spy(), md.dates[2], md.dates[4])
    assert seen == [(md.dates[i], md.dates[i]) for i in (1, 2, 3)]


def test_drift_above_cap_is_reported_at_close():
    md = make_md(n_days=6, drift={IDS[1]: 0.30})  # 一檔每天漲 30%
    res = run_backtest(md, EqualWeight(n=25), md.dates[1], md.dates[-1], BacktestConfig(rebalance_every=100))
    assert (res.violations["phase"] == "close").any()
    assert res.violations["problem"].str.contains("超過上限").any()


def test_cash_dividend_offsets_ex_date_drop():
    md0 = make_md(n_days=6)
    ex = md0.dates[3]
    p = md0.prices
    p.loc[(p["stock_id"] == IDS[1]) & (p["date"] >= ex), ["open", "high", "low", "close"]] -= 5.0
    div = pd.DataFrame({
        "stock_id": [IDS[1]], "CashExDividendTradingDate": [ex.date().isoformat()],
        "CashEarningsDistribution": [5.0], "CashStatutorySurplus": [0.0],
        "StockExDividendTradingDate": [""], "StockEarningsDistribution": [0.0], "StockStatutorySurplus": [0.0],
    })
    md = MarketData(prices=p, universe=md0.universe, dividend=div)
    fixed = Fixed(cap_and_normalize(pd.Series(1.0, index=IDS[:25])))  # 固定名單，避免除息後市值排名變動而換股
    with_div = run_backtest(md, fixed, md.dates[1], md.dates[-1], BacktestConfig(band=0.05))
    no_div = run_backtest(md, fixed, md.dates[1], md.dates[-1], BacktestConfig(band=0.05, credit_dividends=False))
    held = with_div.holdings.query("stock_id == @IDS[1]")["shares"].iloc[0]
    assert with_div.daily["dividends"].sum() == pytest.approx(held * 5.0)
    assert with_div.daily["nav"].iloc[-1] - no_div.daily["nav"].iloc[-1] == pytest.approx(held * 5.0)


def test_cap_weighted_respects_caps():
    md = make_md()
    res = run_backtest(md, CapWeighted(n=30), md.dates[1], md.dates[-1])
    assert res.violations.empty
    w = res.holdings[res.holdings["date"] == md.dates[-1]].set_index("stock_id")["weight"]
    assert w["2330"] <= 0.25 and w.drop("2330").max() <= 0.10


def test_untradable_stock_is_never_picked():
    md0 = make_md(n_days=5)
    p = md0.prices
    p = p[~((p["stock_id"] == "2330") & (p["date"] >= md0.dates[2]))]  # 2330 第 3 天起停止交易
    md = MarketData(prices=p, universe=md0.universe)
    assert "2330" not in md.view(md.dates[3]).tradable()
    res = run_backtest(md, CapWeighted(n=30), md.dates[3], md.dates[4])
    assert "2330" not in set(res.trades["stock_id"])


def test_leftover_cash_is_reinvested_after_rounding():
    # 高價股：一張 = 淨值的 0.6%，無條件捨去會留下大量現金
    px = pd.Series(6000.0, index=IDS)
    target = cap_and_normalize(pd.Series(1.0, index=IDS[:25]))
    orders = build_orders({}, target, px, px, RULES.initial_capital)
    spent = sum(d * 6000.0 for d in orders.values()) * (1 + RULES.fee_rate)
    cash_left = RULES.initial_capital - spent
    assert 0.02 * RULES.initial_capital <= cash_left < 0.02 * RULES.initial_capital + 6_000_000 * 1.01


def test_cash_does_not_creep_up_with_band():
    md = make_md(n_days=25, drift={s: (0.01 if i % 2 else -0.01) for i, s in enumerate(IDS)})
    res = run_backtest(md, Fixed(cap_and_normalize(pd.Series(1.0, index=IDS[:25]))), md.dates[1], md.dates[-1],
                       BacktestConfig(band=0.01))
    assert res.daily["cash_weight"].max() < 0.02 + 0.01 + 0.005


def test_zero_volume_day_is_not_traded():
    md0 = make_md(n_days=5)
    p = md0.prices
    halted = (p["stock_id"] == IDS[3]) & (p["date"] == md0.dates[2])
    p.loc[halted, "volume"] = 0.0  # 有價格但停牌
    md = MarketData(prices=p, universe=md0.universe)
    res = run_backtest(md, Fixed(cap_and_normalize(pd.Series(1.0, index=IDS[:25]))), md.dates[2], md.dates[3])
    day1 = res.trades[res.trades["date"] == md.dates[2]]
    assert IDS[3] not in set(day1["stock_id"])
    assert IDS[3] in set(res.trades.loc[res.trades["date"] == md.dates[3], "stock_id"])


def test_halted_holding_counts_toward_max_holdings():
    px = pd.Series(100.0, index=IDS)
    px[IDS[0]] = float("nan")  # 持有中但停牌
    current = {IDS[0]: 100_000}
    target = cap_and_normalize(pd.Series(1.0, index=IDS[1:]))  # 換成另外 29 檔……
    target = pd.concat([target, pd.Series({IDS[-1]: 0.0})])
    mark = pd.Series(100.0, index=IDS)
    orders = build_orders(current, target, px, mark, 990_000_000)
    final = {s for s in IDS if current.get(s, 0) + orders.get(s, 0) > 0}
    assert len(final) <= RULES.max_holdings
    assert IDS[0] in final  # 停牌賣不掉
