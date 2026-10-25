"""每日資料健康檢查。

自動化流程最危險的失敗是「沒有報錯、但其實用了舊資料」。這裡把每項輸入的新鮮度明確檢查一遍：
價格不是最新交易日就直接中止（fatal）；其他落後的項目寫進 context 與報告提醒。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from ..data.view import MarketData

PRICES_READY_HOUR = 14.5  # 收盤 13:30，資料源約 14:30 後才完整


class StaleDataError(RuntimeError):
    pass


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    fatal: bool = False


def expected_last_trading_day(now: datetime, holidays: set[pd.Timestamp]) -> pd.Timestamp:
    """now 這個時間點，價格資料「應該」已經涵蓋到哪一天。"""
    d = pd.Timestamp(now.date())
    ready = now.hour + now.minute / 60 >= PRICES_READY_HOUR
    if not ready:
        d -= pd.Timedelta(days=1)
    while d.weekday() >= 5 or d in holidays:
        d -= pd.Timedelta(days=1)
    return d


def _closed_days(after: pd.Timestamp, until: pd.Timestamp, holidays: set[pd.Timestamp]) -> list[pd.Timestamp]:
    """after 之後到 until 之間，交易所查不到行情的平日（臨時休市，例如颱風假）。"""
    from ..data.exchange import twse_daily_quotes

    out = []
    for d in pd.bdate_range(after + pd.Timedelta(days=1), until):
        if d not in holidays and twse_daily_quotes(d).empty:
            out.append(d)
    return out


def run_checks(md: MarketData, now: datetime | None = None, holidays: set[pd.Timestamp] | None = None,
               announcements: pd.DataFrame | None = None, check_closures: bool = True) -> list[Check]:
    if holidays is None:
        from ..data.exchange import twse_holidays

        holidays = twse_holidays()
    now = now or datetime.now()
    last = md.dates[-1]
    expected = expected_last_trading_day(now, holidays)
    note = ""
    # 臨時休市頂多連續幾天；落後太久就不是休市，不必逐日查詢
    if last < expected and check_closures and (expected - last).days <= 5:
        closed = _closed_days(last, expected, holidays)
        if closed:
            holidays = holidays | set(closed)
            expected = expected_last_trading_day(now, holidays)
            note = f"（臨時休市：{', '.join(str(d.date()) for d in closed)}）"
    checks = [Check("價格資料最新", last >= expected,
                    f"資料最後一天 {last.date()}，預期 {expected.date()}{note}", fatal=True)]

    vol = md.panels["volume"].loc[last]
    traded = int((vol > 0).sum())
    alive = int(md.panels["close"].loc[:last].iloc[-20:].notna().any().sum())
    checks.append(Check("最新一天有成交的股票數", traded >= 0.9 * alive,
                        f"{traded} / {alive} 檔（近 20 日有交易者）", fatal=traded < 0.5 * alive))

    insti = md.institutional
    if insti.empty:
        checks.append(Check("法人資料", False, "沒有法人資料，法人因子不會被使用"))
    else:
        li = pd.Timestamp(insti["date"].max())
        checks.append(Check("法人資料最新", li >= last, f"法人資料最後一天 {li.date()}"))

    if not md.revenue.empty:
        lr = pd.to_datetime(md.revenue["date"]).max()
        # 每月 10 日前公布上月營收：過了 11 日還沒有上月資料就提醒
        due = (last - pd.offsets.MonthBegin(1)).normalize() if last.day < 11 else last.replace(day=1)
        checks.append(Check("月營收", lr >= due, f"最新一期對應日期 {lr.date()}"))

    if announcements is not None:
        la = pd.to_datetime(announcements["date"]).max() if len(announcements) else None
        ok = la is not None and (last - la).days <= 4
        checks.append(Check("重大訊息累積", ok, f"最新一筆 {la.date() if la is not None else '無'}"))
    return checks


def render_checks_md(checks: list[Check]) -> str:
    return "\n".join(f"- {'✅' if c.ok else ('❌' if c.fatal else '⚠️')} {c.name}：{c.detail}" for c in checks)
