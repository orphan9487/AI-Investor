"""帳本：比賽期間的實際持股與現金。

平台才是真正的成交方。每天有兩種方式更新帳本：
- apply_fills：用資料裡的開盤/收盤價模擬前一天送出的委託（預設）
- reconcile：平台公布持股後，用平台數字覆蓋（以平台為準）
兩者都會記錄在 history 裡，方便事後稽核。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pandas as pd

from ..rules import RULES, CompetitionRules


@dataclass
class Ledger:
    cash: float
    shares: dict[str, int]
    asof: str | None = None  # 帳本反映到哪一天收盤
    history: list[dict] = field(default_factory=list)
    # 比賽規則：除息金額與除權股數不在當天入帳，由主辦方累積到初賽結束才加回淨值
    accrued_cash: float = 0.0
    accrued_shares: dict[str, int] = field(default_factory=dict)

    @classmethod
    def new(cls, rules: CompetitionRules = RULES) -> "Ledger":
        return cls(cash=rules.initial_capital, shares={})

    @classmethod
    def load(cls, path: Path) -> "Ledger":
        d = json.loads(path.read_text(encoding="utf-8"))
        return cls(cash=d["cash"], shares={k: int(v) for k, v in d["shares"].items()},
                   asof=d.get("asof"), history=d.get("history", []),
                   accrued_cash=d.get("accrued_cash", 0.0),
                   accrued_shares={k: int(v) for k, v in d.get("accrued_shares", {}).items()})

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def nav(self, prices: pd.Series) -> float:
        """主辦方每日公布的淨值（不含累積中的除權息）。"""
        return self.cash + sum(n * float(prices[s]) for s, n in self.shares.items())

    def final_nav(self, prices: pd.Series) -> float:
        """初賽結束時的淨值：加回累積的除息金額與除權股數。"""
        return (self.nav(prices) + self.accrued_cash
                + sum(n * float(prices[s]) for s, n in self.accrued_shares.items()))

    def accrue_dividends(self, events: pd.DataFrame, date: str) -> float:
        """events：當天除權息的 stock_id, cash（每股現金股利）, stock_ratio（每股配股數）。回傳累積的現金。"""
        added = 0.0
        for _, ev in events.iterrows():
            n = self.shares.get(ev["stock_id"], 0)
            if n <= 0:
                continue
            added += n * float(ev["cash"])
            extra = int(n * float(ev["stock_ratio"]))
            if extra:
                self.accrued_shares[ev["stock_id"]] = self.accrued_shares.get(ev["stock_id"], 0) + extra
        if added:
            self.accrued_cash += added
            self.history.append({"date": date, "event": "dividend_accrued", "cash": added})
        return added

    def apply_fills(
        self,
        orders: pd.DataFrame,
        fill_prices: pd.Series,
        volume: pd.Series,
        trade_date: str,
        rules: CompetitionRules = RULES,
    ) -> pd.DataFrame:
        """orders 欄位 stock_id, shares（正買負賣）。先賣後買；當天沒成交量的委託視為未成交。
        回傳每筆委託的成交結果。"""
        rows = []
        ordered = orders.sort_values("shares")  # 負數（賣）在前
        for _, o in ordered.iterrows():
            sid, delta = str(o["stock_id"]), int(o["shares"])
            p = fill_prices.get(sid)
            if p is None or pd.isna(p) or not volume.get(sid, 0) > 0:
                rows.append({"stock_id": sid, "shares": delta, "filled": 0, "price": None, "cost": 0.0,
                             "status": "未成交（當日無成交量）"})
                continue
            p = float(p)
            value = abs(delta) * p
            if delta > 0:  # 比賽規則：全數成交；現金若變負，由當日檢查判定整天交易無效
                cost = rules.buy_cost(value)
                self.cash -= value + cost
            else:
                held = self.shares.get(sid, 0)
                if -delta > held:
                    rows.append({"stock_id": sid, "shares": delta, "filled": 0, "price": p, "cost": 0.0,
                                 "status": f"未成交（持股只有 {held}）"})
                    continue
                cost = rules.sell_cost(value)
                self.cash += value - cost
            self.shares[sid] = self.shares.get(sid, 0) + delta
            if self.shares[sid] == 0:
                del self.shares[sid]
            rows.append({"stock_id": sid, "shares": delta, "filled": delta, "price": p, "cost": cost,
                         "status": "成交"})
        result = pd.DataFrame(rows, columns=["stock_id", "shares", "filled", "price", "cost", "status"])
        self.asof = trade_date
        self.history.append({"date": trade_date, "event": "fills", "cash": self.cash,
                             "n_orders": len(orders), "n_filled": int((result["status"] == "成交").sum())})
        return result

    def snapshot(self) -> dict:
        return {"cash": self.cash, "shares": dict(self.shares)}

    def restore(self, snap: dict) -> None:
        self.cash, self.shares = snap["cash"], dict(snap["shares"])

    def reconcile(self, cash: float, shares: dict[str, int], asof: str, note: str = "") -> dict:
        """用平台公布的持股覆蓋帳本，回傳差異。"""
        diff = {
            "cash": cash - self.cash,
            "shares": {s: shares.get(s, 0) - self.shares.get(s, 0)
                       for s in set(shares) | set(self.shares) if shares.get(s, 0) != self.shares.get(s, 0)},
        }
        self.cash, self.shares, self.asof = cash, {s: n for s, n in shares.items() if n}, asof
        self.history.append({"date": asof, "event": "reconcile", "diff": diff, "note": note})
        return diff
