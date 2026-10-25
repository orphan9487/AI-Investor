"""比賽規則常數與合規檢查。

規則來源：AI CUP 2026 玉山人工智慧公開挑戰賽（Agent 基金經理人）官網與公告。
官網若有更新，只改這個檔案。回測引擎與實盤交易書產生器共用同一份檢查。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class CompetitionRules:
    initial_capital: float = 1_000_000_000.0
    min_holdings: int = 20
    max_holdings: int = 30
    tsmc_id: str = "2330"
    tsmc_max_weight: float = 0.25
    stock_max_weight: float = 0.10
    max_cash_weight: float = 0.25  # 現金部位需「低於」淨值 25%
    lot_size: int = 1000  # 只能交易整股（一張 = 1000 股）
    fee_rate: float = 0.001425  # 買賣皆收
    sell_tax_rate: float = 0.003  # 賣出證交稅
    max_daily_submissions: int = 25

    def cap_for(self, stock_id: str) -> float:
        return self.tsmc_max_weight if stock_id == self.tsmc_id else self.stock_max_weight

    def buy_cost(self, value: float) -> float:
        return value * self.fee_rate

    def sell_cost(self, value: float) -> float:
        return value * (self.fee_rate + self.sell_tax_rate)


RULES = CompetitionRules()

# 權重上限往下預留的緩衝。收盤漂移是否算違規尚未確認，先保守一點。
DEFAULT_CAP_BUFFER = 0.005


def check_portfolio(
    shares: Mapping[str, int],
    prices: Mapping[str, float],
    cash: float,
    universe: set[str] | None = None,
    rules: CompetitionRules = RULES,
) -> list[str]:
    """回傳違規描述清單；空清單代表合規。

    shares: 每檔持有股數（0 的會被忽略）
    prices: 用來估值的價格（通常是收盤價或成交價）
    """
    problems: list[str] = []
    held = {sid: n for sid, n in shares.items() if n != 0}

    for sid, n in held.items():
        if n < 0:
            problems.append(f"{sid} 持股為負（{n}），不允許放空")
        if n % rules.lot_size != 0:
            problems.append(f"{sid} 持股 {n} 不是整張（{rules.lot_size} 股）的倍數")
        if universe is not None and sid not in universe:
            problems.append(f"{sid} 不在標的池內")
        if sid not in prices or not prices[sid] > 0:
            problems.append(f"{sid} 沒有有效價格，無法估值")

    if cash < 0:
        problems.append(f"現金為負（{cash:,.0f}），不允許融資")

    n_held = sum(1 for n in held.values() if n > 0)
    if not rules.min_holdings <= n_held <= rules.max_holdings:
        problems.append(
            f"持股檔數 {n_held} 不在 {rules.min_holdings}–{rules.max_holdings} 之間"
        )

    stock_value = sum(n * prices.get(sid, 0.0) for sid, n in held.items())
    nav = cash + stock_value
    if nav <= 0:
        problems.append("淨值 <= 0")
        return problems

    for sid, n in held.items():
        w = n * prices.get(sid, 0.0) / nav
        cap = rules.cap_for(sid)
        if w > cap + 1e-12:
            problems.append(f"{sid} 權重 {w:.2%} 超過上限 {cap:.0%}")

    cash_w = cash / nav
    if cash_w >= rules.max_cash_weight:
        problems.append(f"現金比重 {cash_w:.2%} 未低於 {rules.max_cash_weight:.0%}")

    return problems
