"""每日流程。

  prepare   更新資料 → 用最新成交日的價格結算前一次委託 → 量化評分 → 產出 context（給 Claude）
  (Claude)  讀 context、查 TradingView、寫 decision.json
  finalize  驗證 decision → 套用 → 整張下單 → 壓力測試與合規檢查 → 交易書、決策報告

每個交易日一個資料夾 runs/<交易日>/，所有輸入輸出與日誌都留在裡面，供審查與複賽簡報使用。
"""
from __future__ import annotations

import hashlib
import json
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
from .ledger import Ledger

RUNS_DIR = store.PROJECT_ROOT / "runs"
LEDGER_PATH = store.PROJECT_ROOT / "state" / "ledger.json"

COMPETITION_START = pd.Timestamp("2026-10-26")
COMPETITION_END = pd.Timestamp("2026-11-27")
ANNOUNCEMENT_DAYS = 7


@dataclass
class AgentConfig:
    exec_price: str = "open"  # 官方成交價基準確認前的假設
    band: float = 0.01
    stress_gap: float = 0.03  # 隔日開盤全面上漲 3% 時，買進金額仍不能超過現金
    strategy: CompositeScore | None = None

    def make_strategy(self) -> CompositeScore:
        return self.strategy or CompositeScore()


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

def settle_pending(md: MarketData, ledger: Ledger, cfg: AgentConfig) -> list[str]:
    """把交易日 <= 最新資料日、且尚未結算的委託，依序用當天價格成交。回傳結算的交易日。"""
    settled = []
    if not RUNS_DIR.exists():
        return settled
    last = md.dates[-1]
    for d in sorted(p.name for p in RUNS_DIR.iterdir() if p.is_dir()):
        status = _read_status(d)
        if status.get("state") != "final" or pd.Timestamp(d) > last:
            continue
        if pd.Timestamp(d) not in md.dates:
            _write_status(d, state="skipped", note="資料中沒有這個交易日")
            continue
        orders = pd.read_csv(run_dir(d) / "orders.csv", dtype={"stock_id": str})
        ts = pd.Timestamp(d)
        fills = ledger.apply_fills(orders[["stock_id", "shares"]], md.panels[cfg.exec_price].loc[ts],
                                   md.panels["volume"].loc[ts], d)
        fills.to_csv(run_dir(d) / "fills.csv", index=False, encoding="utf-8-sig")
        _write_status(d, state="settled", n_filled=int((fills["status"] == "成交").sum()), n_orders=len(orders))
        _log(d, "settled", cash=ledger.cash, n_holdings=len(ledger.shares))
        settled.append(d)
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
            trade_date: str | None = None) -> Path:
    cfg = cfg or AgentConfig()
    if update:
        from ..data.daily import daily_update

        daily_update()
    md = md or MarketData.load()
    asof = md.dates[-1]
    trade_date = trade_date or next_trading_day(asof).date().isoformat()
    d = run_dir(trade_date)
    d.mkdir(parents=True, exist_ok=True)

    ledger = load_ledger()
    settled = settle_pending(md, ledger, cfg)
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
    from ..data.daily import load_announcements

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
        "announcements": announcements,
        "ledger_shares": ledger.shares,
        "strategy": {k: v for k, v in vars(strat).items() if not k.startswith("_")},
    }
    (d / "context.json").write_text(json.dumps(ctx, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (d / "context.md").write_text(render_context_md(ctx), encoding="utf-8")
    _write_status(trade_date, state="prepared", data_asof=ctx["data_asof"])
    _log(trade_date, "prepared", data_asof=ctx["data_asof"], nav=nav, settled=settled,
         context_sha=_sha(d / "context.json"))
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
        "## 量化基準組合（finalize 會以此為起點）",
        "",
        _table(ctx["base_portfolio"]),
        "",
        f"## 候選名單（排名 <= {CANDIDATE_RANK}、不在基準組合，可用 include 加入）",
        "",
        _table(ctx["reserve_candidates"]),
    ]
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

def finalize(trade_date: str, cfg: AgentConfig | None = None, md: MarketData | None = None) -> Path:
    cfg = cfg or AgentConfig()
    d = run_dir(trade_date)
    ctx = json.loads((d / "context.json").read_text(encoding="utf-8"))
    md = md or MarketData.load(with_finmind=False)
    asof = pd.Timestamp(ctx["data_asof"])
    close = md.view(asof).last("close")
    ledger = load_ledger()

    base = pd.Series({r["stock_id"]: r["base_weight"] for r in ctx["base_portfolio"]}, dtype=float)
    cand_rows = ctx["base_portfolio"] + ctx["reserve_candidates"]
    candidates = pd.DataFrame(cand_rows).set_index("stock_id").sort_values("rank")

    warnings: list[str] = []
    dec_path = d / "decision.json"
    if dec_path.exists():
        try:
            obj = json.loads(dec_path.read_text(encoding="utf-8"))
            decision, warnings = parse_decision(obj, trade_date, base, candidates)
            _log(trade_date, "decision_loaded", sha=_sha(dec_path), warnings=warnings)
        except (json.JSONDecodeError, TypeError, AttributeError) as e:
            decision = fallback(trade_date, f"decision.json 無法解析：{e}")
            warnings = [decision.notes]
    else:
        decision = fallback(trade_date, "沒有 decision.json")
        warnings = [decision.notes]

    final, adjustments = apply_decision(decision, base, candidates)

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
    (d / "decision_validated.json").write_text(
        json.dumps({"decision": decision.to_dict(), "warnings": warnings}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    from .report import render_report

    report = render_report(ctx, decision, warnings, adjustments, order_df, target_df,
                           post_cash, nav, problems, cfg)
    (d / "report.md").write_text(report, encoding="utf-8")

    state = "final" if not problems else "blocked"
    _write_status(trade_date, state=state, author=decision.author, n_orders=len(order_df),
                  problems=problems)
    _log(trade_date, "finalized", state=state, author=decision.author, n_orders=len(order_df),
         problems=problems, orders_sha=_sha(d / "orders.csv"))
    return d


def _cash_flow(orders: dict[str, int], prices: pd.Series, gap: float = 0.0) -> float:
    flow = 0.0
    for s, n in orders.items():
        if n > 0:
            v = n * float(prices[s]) * (1 + gap)
            flow -= v + RULES.buy_cost(v)
        else:
            v = -n * float(prices[s])
            flow += v - RULES.sell_cost(v)
    return flow


def _stress_cash(ledger: Ledger, orders: dict[str, int], prices: pd.Series, gap: float) -> float:
    return ledger.cash + _cash_flow(orders, prices, gap)
