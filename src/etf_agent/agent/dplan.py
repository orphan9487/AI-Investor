"""D-Plan（當日 Agent 決策報告及交易書，schema 4.x）的產生與送出前檢核。

主辦方收件時會：
- 驗 schema 與引用鏈（sources → observations → inferences → decisions / no_trade_decisions → orders）
- 驗決策覆蓋：前一日每檔持股都要出現在 decisions 或 no_trade_decisions
- 用官方推導重算委託：target_shares = floor(target_weight × nav_tradable / T-1 收盤價 / 1000) × 1000，
  與 orders 比對 ticker / side / shares
- 用 T-1 收盤價重算淨流向與委託後現金比，比對 market_view.posture
本模組在送出前把以上全部先檢查一遍。
"""
from __future__ import annotations

import json
import math
import re
import subprocess
from datetime import datetime
from pathlib import Path

import pandas as pd

from ..data import store
from ..rules import RULES

SCHEMA_PATH = store.PROJECT_ROOT / "templates" / "D-Plan.schema.json"
SCHEMA_VERSION = "4.3"
HOLD_TOLERANCE = 0.02  # net_exposure_intent=hold 時，|淨流向| / NAV 的容差（server 預設 2%）
LOT = RULES.lot_size

SOURCE_URLS = {
    "twse_quotes": ("twse", "TWSE 每日收盤行情（含成交金額、成交均價）",
                    "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={ymd}&type=ALLBUT0999&response=json"),
    "tpex_quotes": ("tpex", "TPEx 上櫃股票行情",
                    "https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes?date={ymd_slash}&response=json"),
    "twse_insti": ("twse", "TWSE 三大法人買賣超日報（T86）",
                   "https://www.twse.com.tw/rwd/zh/fund/T86?date={ymd}&selectType=ALL&response=json"),
    "tpex_insti": ("tpex", "TPEx 三大法人買賣明細",
                   "https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade?date={ymd_slash}&type=Daily&sect=EW&response=json"),
    "twse_revenue": ("mops", "上市公司每月營業收入彙總（公開資訊觀測站，TWSE OpenAPI）",
                     "https://openapi.twse.com.tw/v1/opendata/t187ap05_L"),
    "tpex_revenue": ("mops", "上櫃公司每月營業收入彙總（公開資訊觀測站，TPEx OpenAPI）",
                     "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O"),
}


def plan_filename(team_id: str, trade_date: str) -> str:
    """官方檔名規約：D-Plan_<team_id>_<trade_date>.json（只用 A-Z a-z 0-9 _ -，不帶時間戳）。"""
    return f"D-Plan_{team_id}_{trade_date}.json"


def save_evidence(folder: Path, plan: dict, md, asof, close, ctx: dict) -> None:
    """留存 observations 用到的原始數據（主辦方會抽檢數字是否對得上來源）。"""
    folder.mkdir(parents=True, exist_ok=True)
    tickers = sorted({d["ticker"] for d in plan["decisions"]} | {n["ticker"] for n in plan["no_trade_decisions"]})
    tickers = [t for t in tickers if t in md.stock_ids]
    asof = pd.Timestamp(asof)
    win = md.panels["close"].loc[:asof].iloc[-70:].index
    prices = pd.concat({f: md.panels[f].loc[win, tickers] for f in ("close", "adj_close", "volume", "vwap")},
                       names=["field"]).stack(future_stack=True).rename("value").reset_index()
    prices.to_csv(folder / "prices_last70d.csv", index=False, encoding="utf-8-sig")
    pd.Series({t: float(close[t]) for t in tickers}, name="official_close_prev").rename_axis("stock_id") \
        .to_csv(folder / "official_close_prev.csv", encoding="utf-8-sig")
    if not md.revenue.empty:
        rev = md.revenue[md.revenue["stock_id"].isin(tickers)].sort_values(["stock_id", "date"])
        rev.groupby("stock_id").tail(16).to_csv(folder / "monthly_revenue.csv", index=False, encoding="utf-8-sig")
    if not md.institutional.empty:
        ins = md.institutional[md.institutional["stock_id"].isin(tickers)]
        ins = ins[pd.to_datetime(ins["date"]) > asof - pd.Timedelta(days=40)]
        ins.to_csv(folder / "institutional_40d.csv", index=False, encoding="utf-8-sig")
    rows = {r["stock_id"]: r for r in ctx["base_portfolio"] + ctx["reserve_candidates"] + ctx["held_not_in_base"]}
    (folder / "factor_rows.json").write_text(json.dumps({t: rows.get(t) for t in tickers}, ensure_ascii=False,
                                                        indent=1, default=str), encoding="utf-8")
    (folder / "README.txt").write_text(
        "observations 的數值來源：收盤價＝交易所前日收盤（official_close_prev.csv）；"
        "近 60 日報酬＝adj_close（含除權息還原）第 -6 天 / 第 -66 天 - 1；"
        "營收年增＝monthly_revenue.csv 最近 3 期與去年同月比的平均；"
        "法人＝institutional_40d.csv 外資＋投信最近 20 個交易日淨買超 / 發行股數。\n", encoding="utf-8")


def ts(dt: datetime | pd.Timestamp | str, hhmm: str | None = None) -> str:
    """台北時間 ISO-8601（+08:00）。"""
    t = pd.Timestamp(dt)
    if hhmm:
        h, m = map(int, hhmm.split(":"))
        t = t.normalize() + pd.Timedelta(hours=h, minutes=m)
    return t.strftime("%Y-%m-%dT%H:%M:%S") + "+08:00"


# ---------------------------------------------------------------- 推導

def target_shares(weight: float, nav: float, price: float) -> int:
    """官方推導：floor(target_weight × nav_tradable / 前日收盤價 / 1000) × 1000。"""
    return int(math.floor(weight * nav / price / LOT + 1e-9)) * LOT


def weight_for_lots(shares: int, price: float, nav: float) -> float:
    """反推 target_weight：取「目標張數 + 半張」對應的權重，讓 floor 穩定落在目標張數。"""
    if shares <= 0:
        return 0.0
    w = round((shares + LOT / 2) * price / nav, 8)
    assert target_shares(w, nav, price) == shares, (shares, price, nav, w)
    return w


def derive_orders(decisions: list[dict], prev_shares: dict[str, int], nav: float, close: dict[str, float]) -> list[dict]:
    out = []
    for d in decisions:
        t = d["ticker"]
        tgt = 0 if d["action"] == "SELL_ALL" else target_shares(d["target_weight"], nav, close[t])
        delta = tgt - prev_shares.get(t, 0)
        if delta:
            out.append({"ticker": t, "side": "BUY" if delta > 0 else "SELL", "shares": abs(delta),
                        "decision_ref": d["decision_id"]})
    return out


def code_version() -> str:
    try:
        h = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=store.PROJECT_ROOT,
                           capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=store.PROJECT_ROOT,
                               capture_output=True, text=True).stdout.strip()
        return f"git:{h}" + ("-dirty" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return "unversioned"


# ---------------------------------------------------------------- 組裝

class _Ids:
    def __init__(self, prefix: str):
        self.prefix, self.n = prefix, 0

    def next(self) -> str:
        self.n += 1
        return f"{self.prefix}{self.n}"


def _pct(x) -> str:
    return "—" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x * 100:+.1f}%"


def build(
    *,
    ctx: dict,
    llm: dict | None,
    decision,  # agent.decision.Decision（已驗證）
    prev_shares: dict[str, int],
    post_shares: dict[str, int],
    nav: float,
    close: dict[str, float],
    cash: float,
    markets: dict[str, str],
    names: dict[str, str],
    taiex: dict | None,
    team_id: str,
    model_version: str,
    run_started_at: str,
) -> dict:
    trade_date, asof = ctx["trade_date"], pd.Timestamp(ctx["data_asof"])
    ymd, ymd_slash = asof.strftime("%Y%m%d"), asof.strftime("%Y/%m/%d")
    now = ts(datetime.now())
    S, O, I, D = _Ids("S"), _Ids("O"), _Ids("I"), _Ids("D")
    sources, observations, inferences, decisions, no_trade = [], [], [], [], []

    # L0：官方資料來源
    src_id = {}
    as_of_time = {"twse_quotes": "14:30", "tpex_quotes": "14:30", "twse_insti": "16:30", "tpex_insti": "16:30",
                  "twse_revenue": "18:00", "tpex_revenue": "18:00"}
    for key, (auth, name, url) in SOURCE_URLS.items():
        sid = S.next()
        src_id[key] = sid
        sources.append({"source_id": sid, "authority": auth, "name": name,
                        "url": url.format(ymd=ymd, ymd_slash=ymd_slash),
                        "content_as_of": ts(asof, as_of_time[key]), "fetched_at": now})

    # L0／L1：LLM 提供的來源與觀察（重新編號）
    llm = llm or {}
    llm_src, llm_obs = {}, {}
    for s in llm.get("sources") or []:
        try:
            sid = S.next()
            entry = {"source_id": sid, "authority": s["authority"], "url": s["url"],
                     "content_as_of": ts(s["content_as_of"])}
            for k in ("name", "archive_url"):
                if s.get(k):
                    entry[k] = str(s[k])[:300]
            if s.get("published_at"):
                entry["published_at"] = ts(s["published_at"])
            entry["fetched_at"] = ts(s.get("fetched_at") or datetime.now())
            sources.append(entry)
            llm_src[str(s.get("id"))] = sid
        except (KeyError, ValueError, TypeError):
            S.n -= 1
    for o in llm.get("observations") or []:
        refs = [llm_src[r] for r in o.get("source") or [] if r in llm_src]
        vals = {k: float(v) for k, v in (o.get("values") or {}).items() if isinstance(v, (int, float))}
        if not refs or not vals or len(str(o.get("statement", ""))) < 5:
            continue
        oid = O.next()
        entry = {"obs_id": oid, "source_ref": refs, "statement": str(o["statement"])[:500], "values": vals}
        if o.get("topic") and re.fullmatch(r"[a-z][a-z0-9_]{1,40}", str(o["topic"])):
            entry["topic"] = o["topic"]
        observations.append(entry)
        llm_obs[str(o.get("id"))] = oid

    # L1：大盤觀察（官方）
    market_obs = []
    if taiex:
        oid = O.next()
        observations.append({
            "obs_id": oid, "source_ref": [src_id["twse_quotes"]], "topic": "tw_market",
            "statement": f"{asof.date()} 加權指數收 {taiex['close']:,.2f}（{taiex['chg_pct']:+.2f}%）",
            "values": {"TAIEX": round(taiex["close"], 2), "TAIEX_chg_pct": round(taiex["chg_pct"], 2)}})
        market_obs.append(oid)

    # L1：個股量化觀察（官方資料計算）
    rows = {r["stock_id"]: r for r in ctx["base_portfolio"] + ctx["reserve_candidates"] + ctx["held_not_in_base"]}
    stock_obs = {}
    involved = sorted(set(prev_shares) | {s for s, n in post_shares.items() if n})
    for sid in involved:
        r = rows.get(sid, {})
        mk = markets.get(sid, "twse")
        vals = {"close_prev": float(close[sid])}
        parts = [f"{sid} {names.get(sid, '')}：{asof.date()} 收盤 {close[sid]:,.2f} 元"]
        refs = [src_id[f"{mk}_quotes"]]
        if r.get("mom_raw") is not None:
            vals["ret_60d_skip5_pct"] = round(r["mom_raw"] * 100, 2)
            parts.append(f"近 60 日報酬（不含最近 5 日）{_pct(r['mom_raw'])}")
        if r.get("rev_yoy_3m") is not None:
            vals["rev_yoy_3m_avg_pct"] = round(r["rev_yoy_3m"] * 100, 2)
            parts.append(f"近 3 個月營收年增率平均 {_pct(r['rev_yoy_3m'])}")
            refs.append(src_id[f"{mk}_revenue"])
        if r.get("flow_raw") is not None:
            vals["foreign_trust_net_20d_pct_of_shares"] = round(r["flow_raw"] * 100, 4)
            parts.append(f"外資＋投信近 20 日淨買超占發行股數 {r['flow_raw'] * 100:+.3f}%")
            refs.append(src_id[f"{mk}_insti"])
        if r.get("rank") is not None:
            vals["composite_rank"] = float(r["rank"])
            parts.append(f"三因子綜合排名第 {r['rank']} 名")
        oid = O.next()
        observations.append({"obs_id": oid, "source_ref": refs, "topic": "stock_factors",
                             "statement": "；".join(parts)[:500], "values": vals})
        stock_obs[sid] = oid

    # L2／L3：每檔一筆推論，再接決策或續抱
    strat = ctx.get("strategy", {})
    keep_rank, n_target = strat.get("keep_rank", 40), strat.get("n", 25)
    band = ctx.get("assumptions", {}).get("band", 0.01)
    llm_actions = {a.stock_id: a for a in decision.tilts + decision.exclude + decision.include}
    for sid in involved:
        prev, post = prev_shares.get(sid, 0), post_shares.get(sid, 0)
        r = rows.get(sid, {})
        rank = r.get("rank")
        w_post = post * close[sid] / nav
        a = llm_actions.get(sid)
        premise = [stock_obs[sid]] + [llm_obs[e] for e in (getattr(a, "evidence", None) or []) if e in llm_obs][:9]

        if sid == RULES.tsmc_id and post > 0:
            logic = (f"台積電為本 ETF 核心持股，策略規則固定目標權重 {strat.get('tsmc_weight', 0.22):.0%}，"
                     f"低於 25% 上限以吸收股價波動；本日目標權重 {w_post:.2%}")
        elif post == 0 and prev > 0:
            logic = (f"{sid} 三因子綜合排名第 {rank} 名，已跌出續抱門檻（前 {keep_rank} 名），依選股規則出清"
                     if rank else f"{sid} 已不符合流動性或可交易條件，依選股規則出清")
        elif prev == 0:
            logic = (f"依本 ETF 多因子規則（股價動能、營收成長、法人籌碼），{sid} 綜合排名第 {rank} 名，"
                     f"進入前 {n_target} 名選股範圍且產業持股未達上限，新建倉至 {w_post:.2%}")
        elif post == prev:
            logic = (f"{sid} 綜合排名第 {rank} 名，仍在續抱門檻（前 {keep_rank} 名）內；"
                     f"與目標權重的差距小於無交易帶 {band:.0%}，維持不動以節省交易成本")
        else:
            logic = (f"{sid} 綜合排名第 {rank} 名，續抱；目前權重與目標權重差距超過無交易帶 {band:.0%}，"
                     f"{'加碼' if post > prev else '減碼'}回到目標權重 {w_post:.2%}")
        counter = None
        if a is not None:
            logic = f"{logic}。Agent 依新資訊調整：{a.reason}"
            counter = getattr(a, "counter", None)
        if counter is None and r.get("rev_yoy_3m") is not None and r["rev_yoy_3m"] < 0:
            counter = f"近 3 個月營收年增率為負（{_pct(r['rev_yoy_3m'])}），成長動能偏弱"
        if counter is None and r.get("mom_raw") is not None and r["mom_raw"] < 0:
            counter = f"近 60 日股價動能為負（{_pct(r['mom_raw'])}）"
        if counter is None and r.get("flow_raw") is not None and r["flow_raw"] < 0:
            counter = f"外資＋投信近 20 日淨賣超（占發行股數 {r['flow_raw'] * 100:.3f}%），籌碼面不支持"
        iid = I.next()
        inferences.append({"inf_id": iid, "premise_refs": premise, "logic": logic[:1000],
                           "counter_evidence": counter[:500] if counter else None})

        if post == prev:
            no_trade.append({"ticker": sid, "reason_refs": [iid], "reason": logic[:300]})
            continue
        action = "BUY" if prev == 0 else "SELL_ALL" if post == 0 else "ADD" if post > prev else "TRIM"
        cap = RULES.cap_for(sid)
        decisions.append({
            "decision_id": D.next(), "ticker": sid, "action": action,
            "target_weight": weight_for_lots(post, close[sid], nav),
            "inference_refs": [iid],
            "risk_check": f"目標權重 {w_post:.2%}，上限 {cap:.0%}",
        })

    orders = derive_orders(decisions, prev_shares, nav, close)
    for o in orders:
        d = next(x for x in decisions if x["decision_id"] == o["decision_ref"])
        tgt = 0 if d["action"] == "SELL_ALL" else target_shares(d["target_weight"], nav, close[o["ticker"]])
        o["derivation_detail"] = (f"target_shares = floor({d['target_weight']}×{nav:,.0f}/{close[o['ticker']]}/1000)"
                                  f"×1000 = {tgt:,}；現持 {prev_shares.get(o['ticker'], 0):,} → "
                                  f"{o['side']} {o['shares']:,}")[:300]

    # L2M：市場判斷（LLM 提供判斷，部位含意由委託實際結果計算，確保一致）
    flow = net_flow(orders, close)
    est_cash = estimated_cash(cash, orders, close)
    intent = "increase" if flow > HOLD_TOLERANCE * nav else "reduce" if flow < -HOLD_TOLERANCE * nav else "hold"
    cr = est_cash / nav
    rng = [round(max(0.0, cr - 0.01), 4), round(min(0.25, cr + 0.01), 4)]
    stance = {"aggressive": "aggressive", "neutral": "neutral", "cautious": "defensive",
              "defensive": "defensive"}.get(decision.risk_level, "neutral")
    basis = [llm_obs[b] for b in llm.get("market_basis") or [] if b in llm_obs][:10 - len(market_obs)]
    basis = (market_obs + basis) or [observations[0]["obs_id"]]
    logic = decision.market_summary.strip() if len(decision.market_summary.strip()) >= 20 else \
        "本日無足以改變總曝險的市場級訊號，依量化選股規則維持中性部位，現金水位由委託結果決定。"
    market_view = {
        "basis_refs": basis,
        "logic": logic[:1000],
        "regime": llm.get("regime") if llm.get("regime") in ("risk_on", "neutral", "risk_off") else "neutral",
        "stance": stance,
        "posture": {"net_exposure_intent": intent, "target_cash_pct_range": rng},
        "counter_evidence": (str(llm["counter_evidence"])[:500] if llm.get("counter_evidence") else None),
    }
    if isinstance(llm.get("confidence"), (int, float)) and 0 <= llm["confidence"] <= 1:
        market_view["confidence"] = float(llm["confidence"])

    return {
        "schema_version": SCHEMA_VERSION,
        "doc_type": "D-Plan",
        "team_id": team_id,
        "trade_date": trade_date,
        "sources": sources,
        "observations": observations,
        "market_view": market_view,
        "inferences": inferences,
        "decisions": decisions,
        "no_trade_decisions": no_trade,
        "orders": orders,
        "agent_metadata": {
            "model_provider": "anthropic",
            "model_version": model_version,
            "run_started_at": run_started_at,
            "run_completed_at": ts(datetime.now()),
            "code_version": code_version(),
        },
    }


def net_flow(orders: list[dict], close: dict[str, float]) -> float:
    return sum((1 if o["side"] == "BUY" else -1) * o["shares"] * close[o["ticker"]] for o in orders)


def estimated_cash(cash: float, orders: list[dict], close: dict[str, float]) -> float:
    for o in orders:
        v = o["shares"] * close[o["ticker"]]
        cash += -(v + RULES.buy_cost(v)) if o["side"] == "BUY" else v - RULES.sell_cost(v)
    return cash


# ---------------------------------------------------------------- 檢核

def validate(dp: dict, prev_shares: dict[str, int], nav: float, close: dict[str, float], cash: float,
             universe: set[str]) -> tuple[list[str], list[str]]:
    """回傳 (錯誤, 警告)。錯誤＝主辦方會退件或整批作廢。"""
    import jsonschema

    errors, warnings = [], []
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    v = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    for e in sorted(v.iter_errors(dp), key=lambda e: list(e.path)):
        errors.append(f"schema：{'/'.join(map(str, e.path))} {e.message}")

    def ids(items, key):
        got = [x[key] for x in items]
        dup = {x for x in got if got.count(x) > 1}
        if dup:
            errors.append(f"{key} 重複：{sorted(dup)}")
        return set(got)

    S, O, I, D = (ids(dp["sources"], "source_id"), ids(dp["observations"], "obs_id"),
                  ids(dp["inferences"], "inf_id"), ids(dp["decisions"], "decision_id"))
    # 撰寫指南：流水號從 1 開始、連續遞增、不跳號、不加前導零
    for items, key, prefix in ((dp["sources"], "source_id", "S"), (dp["observations"], "obs_id", "O"),
                               (dp["inferences"], "inf_id", "I"), (dp["decisions"], "decision_id", "D")):
        expect = [f"{prefix}{i}" for i in range(1, len(items) + 1)]
        if [x[key] for x in items] != expect:
            errors.append(f"{key} 必須依序為 {prefix}1…{prefix}{len(items)}（連號、不跳號、不加前導零）")
    for o in dp["observations"]:
        for r in o["source_ref"]:
            if r not in S:
                errors.append(f"引用鏈：{o['obs_id']} 引用不存在的 {r}")
    for r in dp["market_view"]["basis_refs"]:
        if r not in O:
            errors.append(f"引用鏈：market_view 引用不存在的 {r}")
    for i in dp["inferences"]:
        for r in i["premise_refs"]:
            if r not in O:
                errors.append(f"引用鏈：{i['inf_id']} 引用不存在的 {r}")
    for d in dp["decisions"]:
        for r in d["inference_refs"]:
            if r not in I:
                errors.append(f"引用鏈：{d['decision_id']} 引用不存在的 {r}（孤兒決策）")
    for n in dp["no_trade_decisions"]:
        for r in n["reason_refs"]:
            if r not in I:
                errors.append(f"引用鏈：續抱 {n['ticker']} 引用不存在的 {r}")
    for o in dp["orders"]:
        if o["decision_ref"] not in D:
            errors.append(f"引用鏈：委託 {o['ticker']} 引用不存在的 {o['decision_ref']}")

    # 決策覆蓋
    dec_t = [d["ticker"] for d in dp["decisions"]]
    ntd_t = [n["ticker"] for n in dp["no_trade_decisions"]]
    for t in set(dec_t) & set(ntd_t):
        errors.append(f"決策覆蓋：{t} 同時出現在 decisions 與 no_trade_decisions")
    for t in {x for x in dec_t + ntd_t if (dec_t + ntd_t).count(x) > 1}:
        errors.append(f"決策覆蓋：{t} 重複")
    for t, n in prev_shares.items():
        if n > 0 and t not in dec_t and t not in ntd_t:
            errors.append(f"決策覆蓋：前日持股 {t} 沒有決策也沒有續抱紀錄")
    for t in ntd_t:
        if prev_shares.get(t, 0) <= 0:
            warnings.append(f"續抱紀錄 {t} 不是前日持股")
    for d in dp["decisions"]:
        prev = prev_shares.get(d["ticker"], 0)
        a = d["action"]
        if (a == "BUY" and prev > 0) or (a in ("ADD", "TRIM", "SELL_ALL") and prev <= 0):
            errors.append(f"決策 {d['decision_id']} {d['ticker']} 動作 {a} 與前日持股 {prev} 不符")
        if d["ticker"] not in universe:
            errors.append(f"{d['ticker']} 不在 150 檔標的池")

    # 推導一致
    missing_px = [d["ticker"] for d in dp["decisions"] if not (close.get(d["ticker"]) or 0) > 0]
    if missing_px:
        errors.append(f"缺前日收盤價：{missing_px}")
        return errors, warnings
    # 推導比對用「多重集合」：同一檔重複委託不能被集合吃掉
    from collections import Counter

    derived = Counter((o["ticker"], o["side"], o["shares"], o["decision_ref"])
                      for o in derive_orders(dp["decisions"], prev_shares, nav, close))
    given = Counter((o["ticker"], o["side"], o["shares"], o["decision_ref"]) for o in dp["orders"])
    for x in sorted((derived - given).elements()):
        errors.append(f"推導不一致：官方推導有 {x[:3]}（{x[3]}），orders 沒有")
    for x in sorted((given - derived).elements()):
        errors.append(f"推導不一致：orders 有 {x[:3]}（{x[3]}），官方推導沒有")
    tick = Counter(o["ticker"] for o in dp["orders"])
    for t, n in tick.items():
        if n > 1:
            errors.append(f"委託重複：{t} 出現 {n} 筆")
    dec_by_id = {d["decision_id"]: d for d in dp["decisions"]}
    for o in dp["orders"]:
        d = dec_by_id.get(o["decision_ref"])
        if d is None:
            continue
        if d["ticker"] != o["ticker"]:
            errors.append(f"委託 {o['ticker']} 引用的 {d['decision_id']} 是 {d['ticker']}")
        want = "BUY" if d["action"] in ("BUY", "ADD") else "SELL"
        if o["side"] != want:
            errors.append(f"委託 {o['ticker']} 方向 {o['side']} 與決策動作 {d['action']} 不符")
    ordered = Counter(o["decision_ref"] for o in dp["orders"])
    for d in dp["decisions"]:
        if ordered.get(d["decision_id"], 0) == 0:
            errors.append(f"決策 {d['decision_id']} {d['ticker']} 推導後沒有委託（應改列 no_trade_decisions）")

    # funding_for：①指向同日 BUY/ADD ②本身是 TRIM/SELL_ALL ③拿掉調度賣出後現金會跌破宣告下限
    funding = [d for d in dp["decisions"] if d.get("funding_for")]
    for d in funding:
        if d["action"] not in ("TRIM", "SELL_ALL"):
            errors.append(f"funding_for 只能用在 TRIM/SELL_ALL，{d['decision_id']} 是 {d['action']}")
        for ref in d["funding_for"]:
            tgt = dec_by_id.get(ref)
            if tgt is None or tgt["action"] not in ("BUY", "ADD"):
                errors.append(f"{d['decision_id']} 的 funding_for 指向 {ref}，不是同日的 BUY/ADD 決策")
    if funding:
        ids = {d["decision_id"] for d in funding}
        without = [o for o in dp["orders"] if o["decision_ref"] not in ids]
        lo = dp["market_view"]["posture"]["target_cash_pct_range"][0]
        if estimated_cash(cash, without, close) / nav >= lo:
            errors.append("funding_for 宣告的資金缺口不存在：拿掉調度賣出後現金比仍不低於宣告下限")

    # 部位宣告與委託一致
    flow = net_flow(dp["orders"], close)
    intent = dp["market_view"]["posture"]["net_exposure_intent"]
    ok = (intent == "increase" and flow > 0) or (intent == "reduce" and flow < 0) or \
         (intent == "hold" and abs(flow) <= HOLD_TOLERANCE * nav)
    if not ok:
        errors.append(f"部位宣告 {intent} 與委託淨流向 {flow / nav:+.2%} NAV 不一致")
    lo, hi = dp["market_view"]["posture"]["target_cash_pct_range"]
    est = estimated_cash(cash, dp["orders"], close)
    if lo > hi:
        errors.append("target_cash_pct_range 下限大於上限")
    if not lo <= est / nav <= hi:
        errors.append(f"預估現金比 {est / nav:.2%} 不在宣告區間 [{lo:.2%}, {hi:.2%}]")
    if est < 0:
        errors.append(f"以前日收盤價估算現金為負（{est:,.0f}），會被判資金不足")

    # 委託後持股（以前日收盤價估）
    post = dict(prev_shares)
    for o in dp["orders"]:
        post[o["ticker"]] = post.get(o["ticker"], 0) + (o["shares"] if o["side"] == "BUY" else -o["shares"])
        if post[o["ticker"]] < 0:
            errors.append(f"{o['ticker']} 賣出超過持股")
    held = {t: n for t, n in post.items() if n > 0}
    if not RULES.min_holdings <= len(held) <= RULES.max_holdings:
        errors.append(f"委託後持股 {len(held)} 檔，不在 20–30 檔")
    if est / nav > RULES.max_cash_weight:
        errors.append(f"委託後現金比 {est / nav:.2%} 超過 25%")
    bought = {o["ticker"] for o in dp["orders"] if o["side"] == "BUY"}
    for t in bought:
        w = held.get(t, 0) * close[t] / nav
        if w > RULES.cap_for(t):
            errors.append(f"{t} 加碼後權重 {w:.2%} 超過上限 {RULES.cap_for(t):.0%}")

    now = pd.Timestamp(datetime.now())
    for s in dp["sources"]:
        if pd.Timestamp(s["content_as_of"][:19]) > now:
            errors.append(f"{s['source_id']} content_as_of 在未來")
    return errors, warnings
