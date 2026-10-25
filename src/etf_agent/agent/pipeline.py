"""每日流程。

  prepare   更新資料 → 用最新成交日的價格結算前一次委託 → 量化評分 → 產出 context（給 Claude）
  (Claude)  讀 context、查 TradingView、寫 decision.json
  finalize  驗證 decision → 套用 → 整張下單 → 壓力測試與合規檢查 → 交易書、決策報告

每個交易日一個資料夾 runs/<交易日>/，所有輸入輸出與日誌都留在裡面，供審查與複賽簡報使用。
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd

from ..backtest.orders import build_orders
from ..data import store
from ..data.exchange import next_trading_day
from ..data.view import MarketData
from ..rules import RULES, check_portfolio
from ..strategies.composite import CompositeScore
from .decision import CANDIDATE_RANK, apply_decision, fallback, parse_decision
from .diagnostics import concentration, render_concentration_md
from .datapack import DataPackError
from .health import Check, StaleDataError, render_checks_md, run_checks
from .ledger import Ledger

RUNS_DIR = store.PROJECT_ROOT / "runs"
INBOX_DIR = store.PROJECT_ROOT / "inbox"  # 從後台下載的資料包放這裡
LEDGER_PATH = store.PROJECT_ROOT / "state" / "ledger.json"
TEAM_ID_PATH = store.PROJECT_ROOT / "state" / "team_id.txt"
SHADOW_LEDGER_PATH = store.PROJECT_ROOT / "state" / "shadow_ledger.json"  # 純量化影子組合的獨立帳本

COMPETITION_START = pd.Timestamp("2026-10-26")
COMPETITION_END = pd.Timestamp("2026-11-27")
ANNOUNCEMENT_DAYS = 7


@dataclass
class AgentConfig:
    exec_price: str = "vwap"  # 比賽規則：交易日當天的成交均價，全數成交
    band: float = 0.01
    # 成交均價相對前一日收盤：買進全面貴 stress_gap、賣出全面便宜 stress_gap 時，現金仍不能變負
    # （現金為負會記警告且當天交易無效）。依歷史分布校準，見 README
    stress_gap: float = 0.05
    strategy: CompositeScore | None = None
    model_version: str = os.environ.get("ETF_AGENT_MODEL", "claude-opus-5-5")  # D-Plan agent_metadata
    team_id: str = os.environ.get("ETF_TEAM_ID", "")  # 有資料包時以資料包為準

    def make_strategy(self) -> CompositeScore:
        return self.strategy or CompositeScore()


SUBMIT_CUTOFF = (8, 55)  # 交易日當天 08:55 截止；前一個交易日 19:30 開放


def target_trade_date(now: datetime, holidays: set | None = None) -> pd.Timestamp:
    """這次執行是替哪個交易日做決策：08:55 前跑是今天（若今天是交易日），其餘時間是下一個交易日。"""
    from ..data.exchange import twse_holidays

    holidays = twse_holidays() if holidays is None else holidays
    today = pd.Timestamp(now.date())
    before_cutoff = (now.hour, now.minute) < SUBMIT_CUTOFF
    if before_cutoff and today.weekday() < 5 and today not in holidays:
        return today
    return next_trading_day(today, holidays)


SUBMIT_OPEN = (19, 30)  # 交易日「前一日」19:30 開放繳交


def submission_window(now: datetime, holidays: set | None = None) -> tuple[str | None, str]:
    """現在是否在某個交易日的繳交窗口內。回傳 (交易日或 None, 說明)。
    窗口：交易日前一天（日曆日）19:30 ～ 交易日 08:55。"""
    from ..data.exchange import twse_holidays

    holidays = twse_holidays() if holidays is None else holidays
    today = pd.Timestamp(now.date())
    is_trading = lambda d: d.weekday() < 5 and d not in holidays
    if (now.hour, now.minute) < SUBMIT_CUTOFF:
        if is_trading(today):
            return today.date().isoformat(), f"晨間：今天 {today.date()} 是交易日，08:55 截止"
        return None, f"今天 {today.date()} 不是交易日"
    tomorrow = today + pd.Timedelta(days=1)
    if (now.hour, now.minute) >= SUBMIT_OPEN:
        if is_trading(tomorrow):
            return tomorrow.date().isoformat(), f"晚間：明天 {tomorrow.date()} 是交易日，窗口已開放"
        return None, f"明天 {tomorrow.date()} 不是交易日"
    return None, "目前不在任何交易日的繳交窗口（08:55–19:30 之間）"


def archive_run(d: Path) -> Path | None:
    """已產生過 D-Plan 的交易日要重跑時，先把整個 run 目錄原封不動複製到 previous/<時間>_run/
    （previous/ 本身除外；原檔留著供晨間參考）。"""
    import shutil

    if not any(d.glob("D-Plan_*.json")):
        return None
    dst = d / "previous" / f"{datetime.now():%Y%m%d_%H%M%S}_run"
    shutil.copytree(d, dst, ignore=lambda folder, names: ["previous"] if Path(folder) == d else [])
    return dst


def run_dir(trade_date: str) -> Path:
    return RUNS_DIR / trade_date


def _log(trade_date: str, event: str, **data) -> None:
    d = run_dir(trade_date)
    d.mkdir(parents=True, exist_ok=True)
    rec = {"time": datetime.now().isoformat(timespec="seconds"), "event": event, **data}
    with open(d / "log.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def tv_symbol(stock_id: str, market: str) -> str:
    return f"{'TWSE' if market == 'twse' else 'TPEX'}:{stock_id}"


def load_ledger() -> Ledger:
    return Ledger.load(LEDGER_PATH) if LEDGER_PATH.exists() else Ledger.new()


# ---------------------------------------------------------------- 結算前次委託

def day_violations(ledger: Ledger, before: dict, close: pd.Series, universe: set[str]) -> list[str]:
    """依比賽第六節（十四）判定當天交易是否無效。
    個股權重超標：因「加碼」造成的才算；單純價格上漲造成的有 5 個交易日緩衝，這裡只提醒不判無效。"""
    problems = []
    nav = ledger.nav(close)
    if ledger.cash < 0:
        problems.append(f"現金為負（{ledger.cash:,.0f}）")
    elif nav > 0 and ledger.cash / nav > RULES.max_cash_weight:
        problems.append(f"現金比重 {ledger.cash / nav:.2%} 超過 {RULES.max_cash_weight:.0%}")
    n = len(ledger.shares)
    if not RULES.min_holdings <= n <= RULES.max_holdings:
        problems.append(f"持股 {n} 檔")
    for s, k in ledger.shares.items():
        if s not in universe:
            problems.append(f"{s} 不在標的池")
        if k > before["shares"].get(s, 0) and k * float(close[s]) / nav > RULES.cap_for(s):
            problems.append(f"{s} 加碼後權重 {k * float(close[s]) / nav:.2%} 超過上限")
    return problems


def settle_pending(md: MarketData, ledger: Ledger, cfg: AgentConfig) -> list[str]:
    """從帳本最後日期之後，逐個交易日：先累積除權息（以前一日持股計），再用當天成交均價結算該日委託。
    當天結算違反規則時，比照主辦方「當日交易視為無效」還原成前一日持股。回傳結算的交易日。"""
    settled = []
    last = md.dates[-1]
    since = pd.Timestamp(ledger.asof) if ledger.asof else md.dates[0] - pd.Timedelta(days=1)
    events = md.dividend_events
    universe = set(md.stock_ids)
    finals = {p.name for p in RUNS_DIR.iterdir() if p.is_dir() and _read_status(p.name).get("state") == "final"} \
        if RUNS_DIR.exists() else set()

    for d in [x for x in md.dates if since < x <= last]:
        ds = d.date().isoformat()
        if ledger.shares and not events.empty:
            ledger.accrue_dividends(events[events["ex_date"] == d], ds)
        if ds not in finals:
            continue
        orders = pd.read_csv(run_dir(ds) / "orders.csv", dtype={"stock_id": str})
        before = ledger.snapshot()
        fills = ledger.apply_fills(orders[["stock_id", "shares"]], md.panels[cfg.exec_price].loc[d],
                                   md.panels["volume"].loc[d], ds)
        problems = day_violations(ledger, before, md.panels["close"].ffill().loc[d], universe)
        fills.to_csv(run_dir(ds) / "fills.csv", index=False, encoding="utf-8-sig")
        if problems:
            ledger.restore(before)
            ledger.history.append({"date": ds, "event": "invalid_day", "problems": problems})
            _write_status(ds, state="invalid", problems=problems)
            _log(ds, "invalid_day", problems=problems)
        else:
            _write_status(ds, state="settled", n_filled=int((fills["status"] == "成交").sum()),
                          n_orders=len(orders))
            _log(ds, "settled", cash=ledger.cash, n_holdings=len(ledger.shares))
        settled.append(ds)

    # 已提交但資料裡沒有這一天（臨時休市）
    for ds in finals:
        if pd.Timestamp(ds) <= last and pd.Timestamp(ds) not in md.dates:
            _write_status(ds, state="skipped", note="資料中沒有這個交易日（休市？）")
    if ledger.asof is None or pd.Timestamp(ledger.asof) < last:
        ledger.asof = last.date().isoformat()
    return settled


def _read_status(trade_date: str) -> dict:
    p = run_dir(trade_date) / "status.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _write_status(trade_date: str, **kw) -> None:
    st = _read_status(trade_date) | kw | {"updated": datetime.now().isoformat(timespec="seconds")}
    p = run_dir(trade_date) / "status.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- prepare

def prepare(update: bool = True, cfg: AgentConfig | None = None, md: MarketData | None = None,
            trade_date: str | None = None, allow_stale: bool = False, now=None,
            holidays: set | None = None, datapack_path: Path | None = None) -> Path:
    from . import datapack as dpk

    cfg = cfg or AgentConfig()
    if update:
        from ..data.daily import daily_update

        daily_update()
    from ..data.daily import load_announcements

    md = md or MarketData.load()
    asof = md.dates[-1]
    trade_date = trade_date or target_trade_date(now or datetime.now(), holidays).date().isoformat()
    # 資料包的 trade_date 是平台指定的決策日，最權威
    dp = dpk.load(datapack_path) if datapack_path else dpk.find_latest(INBOX_DIR, trade_date)
    if dp is None and not allow_stale:
        bad = dpk.inbox_errors(INBOX_DIR)
        if bad:  # 有資料包卻讀不了：不可默默改用模擬帳本
            raise dpk.DataPackError("inbox/ 有無法讀取的資料包，已停止：" + "；".join(bad))
    if dp is not None and dp.trade_date != trade_date:
        trade_date = dp.trade_date
    d = run_dir(trade_date)
    d.mkdir(parents=True, exist_ok=True)
    archive_run(d)  # 晨間重跑前封存晚間版的完整紀錄，確保每一版 D-Plan 都可重現

    checks = run_checks(md, now=now, holidays=holidays, announcements=load_announcements(),
                        check_closures=not allow_stale)
    fatal = [c for c in checks if c.fatal and not c.ok]
    if fatal and not allow_stale:
        _write_status(trade_date, state="failed", problems=[f"{c.name}：{c.detail}" for c in fatal])
        _log(trade_date, "health_failed", checks=[vars(c) for c in checks])
        raise StaleDataError("；".join(f"{c.name}：{c.detail}" for c in fatal))

    ledger = load_ledger()
    settled = settle_pending(md, ledger, cfg)
    settle_shadow(md, cfg)
    dp_diff = None
    if dp is not None and dp.team_id:  # 記住隊伍代號，之後沒有資料包的日子也能產生正確檔名
        TEAM_ID_PATH.parent.mkdir(parents=True, exist_ok=True)
        TEAM_ID_PATH.write_text(dp.team_id, encoding="utf-8")
    if dp is not None and dp.has_positions:
        # 平台的真實結算覆蓋模擬結算；模擬與平台的差異留紀錄
        dp_diff = dpk.apply_to_ledger(dp, ledger)
        if dp.as_of_date and dp.prev_outcome == "voided":
            _write_status(dp.as_of_date, state="invalid", problems=dp.void_reasons, source="DataPack")
        elif dp.as_of_date and dp.prev_outcome == "executed":
            _write_status(dp.as_of_date, state="settled", source="DataPack", excluded=dp.excluded_codes)
        _log(trade_date, "datapack_applied", source=dp.source, generated_at=dp.generated_at, diff=dp_diff)
    elif dp is not None:
        checks.append(Check("資料包", False, "平台尚無前一交易日結算（missing 含 prev_settlement），改用模擬帳本；"
                                            "結算產生後請重新下載資料包再執行"))
    else:
        bad = dpk.inbox_errors(INBOX_DIR)
        detail = f"inbox/ 找不到 {trade_date} 的資料包，帳本使用模擬結算"
        if bad:
            detail += "；以下檔案無法讀取：" + "；".join(bad)
        checks.append(Check("資料包", False, detail))
    ledger.save(LEDGER_PATH)

    view = md.view(asof)
    close = view.last("close")
    strat = cfg.make_strategy()
    base, scores = strat.select(view, set(ledger.shares))

    uni = md.universe.set_index("stock_id")
    names = uni["name"] if "name" in uni else pd.Series("", index=uni.index)
    nav = ledger.nav(close)
    cur_w = pd.Series({s: n * close[s] / nav for s, n in ledger.shares.items()}, dtype=float)
    perf = view.panel("close", 21)
    ret_1m = perf.iloc[-1] / perf.iloc[0] - 1
    ret_1d = perf.iloc[-1] / perf.iloc[-2] - 1

    def row(s: str) -> dict:
        sc = scores.loc[s] if s in scores.index else pd.Series(dtype=float)
        return {
            "stock_id": s,
            "name": str(names.get(s, "")),
            "tv_symbol": tv_symbol(s, uni.at[s, "market"]),
            "rank": int(sc["rank"]) if "rank" in sc else None,
            "score": round(float(sc["score"]), 3) if "score" in sc else None,
            "base_weight": round(float(base.get(s, 0.0)), 4),
            "current_weight": round(float(cur_w.get(s, 0.0)), 4),
            "close": float(close[s]),
            "ret_1d": round(float(ret_1d.get(s, float("nan"))), 4),
            "ret_1m": round(float(ret_1m.get(s, float("nan"))), 4),
            "mom_raw": _round(sc.get("raw_mom")),
            "rev_yoy_3m": _round(sc.get("raw_rev")),
            "flow_raw": _round(sc.get("raw_flow"), 6),
        }

    order = sorted(base.index, key=lambda s: (-base[s], scores["rank"].get(s, 999)))
    base_rows = [row(s) for s in order]
    reserve = [s for s in scores.index[scores["rank"] <= CANDIDATE_RANK] if s not in base.index]
    held_not_base = [s for s in ledger.shares if s not in base.index]
    watch = set(base.index) | set(reserve) | set(held_not_base)
    ann = load_announcements()
    if not ann.empty:
        ann = ann[ann["stock_id"].isin(watch)
                  & (pd.to_datetime(ann["date"]) > asof - pd.Timedelta(days=ANNOUNCEMENT_DAYS))]
    announcements = [
        {"date": pd.Timestamp(r["date"]).date().isoformat(), "stock_id": r["stock_id"], "name": r["name"],
         "subject": r["subject"], "detail": r.get("detail", "")}
        for _, r in ann.sort_values(["date", "stock_id"], ascending=[False, True]).iterrows()
    ] if not ann.empty else []

    ctx = {
        "trade_date": trade_date,
        "data_asof": asof.date().isoformat(),
        "generated": datetime.now().isoformat(timespec="seconds"),
        "competition": {
            "start": COMPETITION_START.date().isoformat(),
            "end": COMPETITION_END.date().isoformat(),
            "trading_days_left": trading_days_left(trade_date),
        },
        "assumptions": {"exec_price": cfg.exec_price, "band": cfg.band, "stress_gap": cfg.stress_gap},
        "portfolio": {
            "nav": nav,
            "cash": ledger.cash,
            "cash_weight": ledger.cash / nav,
            "return_since_start": nav / RULES.initial_capital - 1,
            "n_holdings": len(ledger.shares),
            "violations": check_portfolio(ledger.shares, close.to_dict(), ledger.cash, set(md.stock_ids)),
            "settled_runs": settled,
        },
        "base_portfolio": base_rows,
        "reserve_candidates": [row(s) for s in reserve],
        "held_not_in_base": [row(s) for s in held_not_base],
        "health": [vars(c) for c in checks],
        "platform": None if dp is None else {
            "generated_at": dp.generated_at, "as_of_date": dp.as_of_date, "missing": dp.missing,
            "prev_outcome": dp.prev_outcome, "void_reasons": dp.void_reasons,
            "excluded_codes": dp.excluded_codes, "violations": dp.violations,
            "notices": dp.compliance.get("notices", 0), "incidents": dp.compliance.get("incidents", []),
            "summary": dpk.summary_lines(dp), "ledger_diff": dp_diff,
            "team_id": dp.team_id,
            "schema_version": dp.schema_version, "trade_date": dp.trade_date,
            "source": dp.source, "sha256": dpk.file_sha256(dp.source),
            "nav_tradable": (dp.account or {}).get("nav_tradable"),
            "cash": (dp.account or {}).get("cash"),
            "holdings": dp.holdings,
        },
        "announcements": announcements,
        "concentration": concentration(view, base),
        "ledger_shares": ledger.shares,
        "strategy": {k: v for k, v in vars(strat).items() if not k.startswith("_")},
    }
    (d / "context.json").write_text(json.dumps(ctx, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (d / "context.md").write_text(render_context_md(ctx), encoding="utf-8")
    _write_status(trade_date, state="prepared", data_asof=ctx["data_asof"])
    _log(trade_date, "prepared", data_asof=ctx["data_asof"], nav=nav, settled=settled,
         context_sha=_sha(d / "context.json"),
         datapack={"source": dp.source, "sha256": dpk.file_sha256(dp.source), "schema_version": dp.schema_version,
                   "team_id": dp.team_id, "trade_date": dp.trade_date, "as_of_date": dp.as_of_date}
         if dp is not None else None)
    return d


def trading_days_left(trade_date: str) -> int:
    """含 trade_date 在內、到比賽結束還有幾個交易日（比賽開始前從開始日算）。"""
    from ..data.exchange import twse_holidays

    start = max(pd.Timestamp(trade_date), COMPETITION_START)
    days = pd.bdate_range(start, COMPETITION_END)
    return int((~days.isin(list(twse_holidays()))).sum())


def _round(x, nd: int = 4):
    return None if x is None or pd.isna(x) else round(float(x), nd)


def render_context_md(ctx: dict) -> str:
    p = ctx["portfolio"]
    lines = [
        f"# 決策材料：交易日 {ctx['trade_date']}（資料截至 {ctx['data_asof']} 收盤）",
        "",
        f"- 比賽期間 {ctx['competition']['start']} ~ {ctx['competition']['end']}，"
        f"含本日剩 {ctx['competition']['trading_days_left']} 個交易日",
        f"- 淨值 {p['nav']:,.0f}（累積 {p['return_since_start']:+.2%}），現金 {p['cash_weight']:.2%}，"
        f"持股 {p['n_holdings']} 檔",
        f"- 目前違規：{'；'.join(p['violations']) if p['violations'] else '無'}",
        f"- 成交價假設：{ctx['assumptions']['exec_price']}",
        "",
        "## 資料健康檢查",
        "",
        render_checks_md([Check(**c) for c in ctx["health"]]),
        "",
        "## 平台資料包（主辦方實際結算與合規狀態）",
        "",
        *([f"- {x}" for x in ctx["platform"]["summary"]] if ctx.get("platform") else ["- （本次沒有資料包）"]),
        "",
        "## 量化基準組合（finalize 會以此為起點）",
        "",
        _table(ctx["base_portfolio"]),
        "",
        f"## 候選名單（排名 <= {CANDIDATE_RANK}、不在基準組合，可用 include 加入）",
        "",
        _table(ctx["reserve_candidates"]),
    ]
    names = {r["stock_id"]: r["name"] for r in ctx["base_portfolio"]}
    lines += ["", "## 基準組合的集中度與相關性", "", render_concentration_md(ctx["concentration"], names)]
    lines += ["", f"## 近 {ANNOUNCEMENT_DAYS} 日重大訊息（公開資訊觀測站，僅列上方股票）", ""]
    if ctx["announcements"]:
        lines += [f"- {a['date']} **{a['stock_id']} {a['name']}**：{a['subject']}" for a in ctx["announcements"]]
        lines += ["", "（完整說明在 context.json 的 announcements[].detail）"]
    else:
        lines.append("（無）")
    if ctx["held_not_in_base"]:
        lines += ["", "## 目前持有但不在基準組合（finalize 會賣出）", "", _table(ctx["held_not_in_base"])]
    return "\n".join(lines) + "\n"


def _table(rows: list[dict]) -> str:
    if not rows:
        return "（無）"
    cols = ["stock_id", "name", "tv_symbol", "rank", "base_weight", "current_weight", "ret_1d", "ret_1m",
            "mom_raw", "rev_yoy_3m"]
    head = "| " + " | ".join(cols) + " |\n|" + "---|" * len(cols)
    body = []
    for r in rows:
        cells = []
        for c in cols:
            v = r.get(c)
            if isinstance(v, float) and c not in ("close",):
                v = f"{v:.2%}" if c != "rank" else v
            cells.append("" if v is None else str(v))
        body.append("| " + " | ".join(cells) + " |")
    return head + "\n" + "\n".join(body)


# ---------------------------------------------------------------- finalize

def official_close(asof: pd.Timestamp, stock_ids: list[str], fallback: pd.Series) -> tuple[pd.Series, list[str]]:
    """交易所公布的 T-1 收盤價（主辦方推導委託用的價格）。查不到的用 fallback 並回報。"""
    from ..data.exchange import tpex_daily_quotes, twse_daily_quotes

    q = pd.concat([twse_daily_quotes(asof), tpex_daily_quotes(asof)], ignore_index=True)
    off = q.dropna(subset=["close"]).drop_duplicates("stock_id").set_index("stock_id")["close"]
    out = fallback.copy()
    hit = off.reindex(stock_ids).dropna()
    out.loc[hit.index] = hit
    return out, sorted(set(stock_ids) - set(hit.index))


def finalize(trade_date: str, cfg: AgentConfig | None = None, md: MarketData | None = None,
             fetch_official: bool = True) -> Path:
    from . import dplan as dpl

    cfg = cfg or AgentConfig()
    d = run_dir(trade_date)
    ctx = json.loads((d / "context.json").read_text(encoding="utf-8"))
    md = md or MarketData.load()  # 含營收與法人：證據快照要用
    asof = pd.Timestamp(ctx["data_asof"])
    close = md.view(asof).last("close")
    ledger = load_ledger()
    data_warnings = []
    taiex = None
    if fetch_official:
        from ..data.exchange import twse_taiex

        close, no_official = official_close(asof, md.stock_ids, close)
        held_missing = [s for s in no_official if s in ledger.shares]
        if held_missing:
            data_warnings.append(f"查不到交易所 {asof.date()} 收盤價、改用資料源價格：{held_missing}")
        taiex = twse_taiex(asof)

    base = pd.Series({r["stock_id"]: r["base_weight"] for r in ctx["base_portfolio"]}, dtype=float)
    cand_rows = ctx["base_portfolio"] + ctx["reserve_candidates"]
    candidates = pd.DataFrame(cand_rows).set_index("stock_id").sort_values("rank")

    warnings: list[str] = list(data_warnings)
    obj = None
    dec_path = d / "decision.json"
    if dec_path.exists():
        try:
            obj = json.loads(dec_path.read_text(encoding="utf-8"))
            decision, parse_warn = parse_decision(obj, trade_date, base, candidates)
            warnings += parse_warn
            _log(trade_date, "decision_loaded", sha=_sha(dec_path), warnings=parse_warn)
        except (json.JSONDecodeError, TypeError, AttributeError) as e:
            obj = None
            decision = fallback(trade_date, f"decision.json 無法解析：{e}")
            warnings.append(decision.notes)
    else:
        decision = fallback(trade_date, "沒有 decision.json")
        warnings.append(decision.notes)

    final, adjustments = apply_decision(decision, base, candidates)
    shadow_final, _ = apply_decision(fallback(trade_date, "影子組合：純量化"), base, candidates)

    # 下單股數以最新收盤價估算；壓力測試：開盤全面上漲 stress_gap 時現金仍要夠
    orders = build_orders(ledger.shares, final, close, close, ledger.cash, band=cfg.band)
    scale = 1.0
    while _stress_cash(ledger, orders, close, cfg.stress_gap) < 0 and scale > 0.8:
        scale -= 0.005
        orders = build_orders(ledger.shares, final * scale, close, close, ledger.cash, band=cfg.band)
        warnings.append(f"壓力測試現金不足，投入比例下修為 {scale:.1%}")

    post = dict(ledger.shares)
    for s, n in orders.items():
        post[s] = post.get(s, 0) + n
    post = {s: n for s, n in post.items() if n}
    post_cash = ledger.cash + _cash_flow(orders, close)
    problems = check_portfolio(post, close.to_dict(), post_cash, set(md.stock_ids))

    uni = md.universe.set_index("stock_id")
    names = uni["name"] if "name" in uni else pd.Series("", index=uni.index)
    order_df = pd.DataFrame([
        {"trade_date": trade_date, "stock_id": s, "name": names.get(s, ""),
         "side": "買進" if n > 0 else "賣出", "shares": n, "lots": abs(n) // RULES.lot_size,
         "ref_price": float(close[s]), "est_value": abs(n) * float(close[s])}
        for s, n in sorted(orders.items(), key=lambda kv: (kv[1] > 0, kv[0]))
    ], columns=["trade_date", "stock_id", "name", "side", "shares", "lots", "ref_price", "est_value"])
    order_df.to_csv(d / "orders.csv", index=False, encoding="utf-8-sig")

    nav = post_cash + sum(n * float(close[s]) for s, n in post.items())
    target_df = pd.DataFrame([
        {"stock_id": s, "name": names.get(s, ""), "shares": n, "lots": n // RULES.lot_size,
         "ref_price": float(close[s]), "weight": n * float(close[s]) / nav}
        for s, n in sorted(post.items(), key=lambda kv: -kv[1] * float(close[kv[0]]))
    ])
    target_df.to_csv(d / "target_holdings.csv", index=False, encoding="utf-8-sig")
    adjustments["name"] = adjustments["stock_id"].map(names)

    # ---- D-Plan（每日唯一繳交物）----
    platform = ctx.get("platform") or {}
    dp_nav = platform.get("nav_tradable") or (ledger.cash + sum(n * float(close[s]) for s, n in ledger.shares.items()))
    dp_cash = platform.get("cash") if platform.get("cash") is not None else ledger.cash
    if not platform.get("nav_tradable"):
        warnings.append("沒有資料包的 nav_tradable，D-Plan 改用帳本估算的淨值；與平台不同時委託推導會不一致，"
                        "請下載資料包後重跑 prepare")
    team_id = platform.get("team_id") or cfg.team_id or         (TEAM_ID_PATH.read_text(encoding="utf-8").strip() if TEAM_ID_PATH.exists() else "")
    if not team_id:
        warnings.append("沒有 team_id（資料包或環境變數 ETF_TEAM_ID），D-Plan 暫填 TEAM_UNKNOWN")
        team_id = "TEAM_UNKNOWN"
    markets = md.universe.set_index("stock_id")["market"].to_dict()
    plan = dpl.build(
        ctx=ctx, llm=obj, decision=decision, prev_shares=dict(ledger.shares), post_shares=post,
        nav=float(dp_nav), close={s: float(v) for s, v in close.items() if pd.notna(v)}, cash=float(dp_cash),
        markets=markets, names=names.to_dict(), taiex=taiex, team_id=team_id,
        model_version=cfg.model_version, run_started_at=dpl.ts(ctx["generated"]),
    )
    dp_errors, dp_warnings = dpl.validate(plan, dict(ledger.shares), float(dp_nav),
                                          {s: float(v) for s, v in close.items() if pd.notna(v)},
                                          float(dp_cash), set(md.stock_ids))
    if team_id == "TEAM_UNKNOWN":
        dp_errors.append("team_id 未知：請先放入資料包（或設定 ETF_TEAM_ID）再重跑，否則檔名與內容都會錯")
    warnings += dp_warnings
    problems += [f"D-Plan：{e}" for e in dp_errors]
    # 官方檔名：D-Plan_<team_id>_<trade_date>.json（同日重繳用同一檔名）。
    # 重跑時前一版（例如晚間版）移到 previous/，供比對與稽核
    plan_path = d / dpl.plan_filename(team_id, trade_date)
    for old in d.glob("D-Plan_*.json"):
        (d / "previous").mkdir(exist_ok=True)
        old.replace(d / "previous" / f"{datetime.now():%Y%m%d_%H%M%S}_{old.name}")
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")

    # 影子組合（純量化，不含 Claude 調整）：同一套下單規則，永久保存當天的目標與委託
    sled = Ledger.load(SHADOW_LEDGER_PATH) if SHADOW_LEDGER_PATH.exists() else Ledger.new()
    shadow_orders = build_orders(sled.shares, shadow_final, close, close, sled.cash, band=cfg.band)
    sc = 1.0
    while _stress_cash(sled, shadow_orders, close, cfg.stress_gap) < 0 and sc > 0.8:
        sc -= 0.005
        shadow_orders = build_orders(sled.shares, shadow_final * sc, close, close, sled.cash, band=cfg.band)
    shadow_post = {s: n for s, n in ((s, sled.shares.get(s, 0) + shadow_orders.get(s, 0))
                                     for s in set(sled.shares) | set(shadow_orders)) if n}
    (d / "shadow.json").write_text(json.dumps({
        "description": "純量化影子組合：同一份資料與下單規則，但不套用 Claude 的調整；委託從影子帳本出發",
        "shadow_ledger_asof": sled.asof, "shadow_cash": sled.cash,
        "target_weights": {s: round(float(w), 6) for s, w in shadow_final.items()},
        "orders": shadow_orders, "post_shares": shadow_post,
        "actual_target_weights": {s: round(float(w), 6) for s, w in final.items()},
        "actual_orders": orders,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    write_manifest(d, ctx, cfg, plan, plan_path)
    dpl.save_evidence(d / "evidence", plan, md, asof, close, ctx)
    (d / "dplan_validation.json").write_text(
        json.dumps({"errors": dp_errors, "warnings": dp_warnings, "n_orders": len(plan["orders"]),
                    "n_decisions": len(plan["decisions"]), "n_no_trade": len(plan["no_trade_decisions"])},
                   ensure_ascii=False, indent=2), encoding="utf-8")

    (d / "decision_validated.json").write_text(
        json.dumps({"decision": decision.to_dict(), "warnings": warnings}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    from .report import render_report

    post_w = target_df.set_index("stock_id")["weight"] if len(target_df) else pd.Series(dtype=float)
    conc = concentration(md.view(asof), post_w) if len(post_w) >= 2 else None
    report = render_report(ctx, decision, warnings, adjustments, order_df, target_df,
                           post_cash, nav, problems, cfg, conc)
    (d / "report.md").write_text(report, encoding="utf-8")

    state = "final" if not problems else "blocked"
    _write_status(trade_date, state=state, author=decision.author, n_orders=len(order_df),
                  problems=problems)
    _log(trade_date, "finalized", state=state, author=decision.author, n_orders=len(order_df),
         problems=problems, orders_sha=_sha(d / "orders.csv"), dplan=plan_path.name, dplan_sha=_sha(plan_path))
    return d


def settle_shadow(md: MarketData, cfg: AgentConfig) -> Ledger:
    """影子帳本：用各交易日 shadow.json 的委託，以同樣的成交均價、作廢規則與除權息處理結算，
    並記錄每天收盤淨值，供 shadow_compare 與實際淨值比較。"""
    led = Ledger.load(SHADOW_LEDGER_PATH) if SHADOW_LEDGER_PATH.exists() else Ledger.new()
    last = md.dates[-1]
    since = pd.Timestamp(led.asof) if led.asof else md.dates[0] - pd.Timedelta(days=1)
    events, universe = md.dividend_events, set(md.stock_ids)
    close_ff = md.panels["close"].ffill()
    for d in [x for x in md.dates if since < x <= last]:
        ds = d.date().isoformat()
        if led.shares and not events.empty:
            led.accrue_dividends(events[events["ex_date"] == d], ds)
        f = run_dir(ds) / "shadow.json"
        if f.exists():
            orders = json.loads(f.read_text(encoding="utf-8"))["orders"]
            if orders:
                df = pd.DataFrame([{"stock_id": s, "shares": n} for s, n in orders.items()])
                before = led.snapshot()
                led.apply_fills(df, md.panels[cfg.exec_price].loc[d], md.panels["volume"].loc[d], ds)
                if day_violations(led, before, close_ff.loc[d], universe):
                    led.restore(before)
        if led.shares or led.history:
            led.history.append({"date": ds, "event": "close_nav", "nav": led.nav(close_ff.loc[d]),
                                "final_nav": led.final_nav(close_ff.loc[d])})
    led.asof = last.date().isoformat()
    led.save(SHADOW_LEDGER_PATH)
    return led


def write_manifest(d: Path, ctx: dict, cfg: AgentConfig, plan: dict, plan_path: Path) -> None:
    """可重播所需的版本與輸入指紋：程式版本、設定、資料包與資料快照的雜湊。"""
    from dataclasses import asdict

    def sha(path: Path) -> str | None:
        return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None

    data_files = [store.PRICES_PATH, store.BENCHMARK_PATH, store.DATA_DIR / "vwap_official.parquet",
                  store.DATA_DIR / "splits.parquet", store.DATA_DIR / "calendar.parquet",
                  store.DATA_DIR / "announcements.parquet", store.UNIVERSE_OFFICIAL_PATH]
    strat = cfg.make_strategy()
    manifest = {
        "trade_date": ctx["trade_date"],
        "data_asof": ctx["data_asof"],
        "code_version": plan["agent_metadata"]["code_version"],
        "model_version": cfg.model_version,
        "agent_config": {k: v for k, v in asdict(cfg).items() if k != "strategy"},
        "strategy": {k: v for k, v in vars(strat).items() if not k.startswith("_")},
        "rules": asdict(RULES),
        "datapack": {k: (ctx.get("platform") or {}).get(k)
                     for k in ("source", "sha256", "schema_version", "team_id", "trade_date", "as_of_date")},
        "data_snapshot_sha256": {p.name: sha(p) for p in data_files},
        "playbook_sha256": sha(store.PROJECT_ROOT / "docs" / "daily_agent_playbook.md"),
        "outputs_sha256": {plan_path.name: sha(plan_path), "shadow.json": sha(d / "shadow.json"),
                           "decision.json": sha(d / "decision.json")},
    }
    (d / "run_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str),
                                         encoding="utf-8")


def _cash_flow(orders: dict[str, int], prices: pd.Series, gap: float = 0.0) -> float:
    """買進以 (1+gap)、賣出以 (1-gap) 倍的價格成交時的現金變化。"""
    flow = 0.0
    for s, n in orders.items():
        if n > 0:
            v = n * float(prices[s]) * (1 + gap)
            flow -= v + RULES.buy_cost(v)
        else:
            v = -n * float(prices[s]) * (1 - gap)
            flow += v - RULES.sell_cost(v)
    return flow


def _stress_cash(ledger: Ledger, orders: dict[str, int], prices: pd.Series, gap: float) -> float:
    return ledger.cash + _cash_flow(orders, prices, gap)
