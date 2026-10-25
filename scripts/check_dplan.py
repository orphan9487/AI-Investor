"""D-Plan 獨立檢查程式：刻意不引用 etf_agent 的任何程式碼，只依《D-Plan 撰寫指南 v4.3》重算一次，
避免產生器與驗證器共用同一個錯誤。

  python scripts/check_dplan.py runs/2026-10-27/D-Plan_TEAM_042_2026-10-27.json inbox/datapack.json
  （官方前日收盤價預設讀同一資料夾的 evidence/official_close_prev.csv，可用 --close 指定）

結束代碼 0＝全部通過；1＝有錯誤（不要上傳）。
"""
import argparse
import csv
import json
import math
import re
import sys
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

FEE, TAX, LOT = 0.001425, 0.003, 1000


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dplan")
    ap.add_argument("datapack")
    ap.add_argument("--close", help="ticker,close 的 CSV（交易所前日收盤價）")
    a = ap.parse_args()

    path = Path(a.dplan)
    dp = json.loads(path.read_text(encoding="utf-8"))
    pack = json.loads(Path(a.datapack).read_text(encoding="utf-8"))
    close_csv = Path(a.close) if a.close else path.parent / "evidence" / "official_close_prev.csv"
    with open(close_csv, encoding="utf-8-sig") as f:
        close = {r[0]: float(r[1]) for r in csv.reader(f) if r and r[0].isdigit()}
    err = []

    # 檔名與身分
    want_name = f"D-Plan_{dp.get('team_id')}_{dp.get('trade_date')}.json"
    if path.name != want_name:
        err.append(f"檔名 {path.name} 應為 {want_name}")
    if dp.get("team_id") != pack.get("team_id"):
        err.append(f"team_id {dp.get('team_id')} 與資料包 {pack.get('team_id')} 不同")
    if dp.get("trade_date") != pack.get("trade_date"):
        err.append(f"trade_date {dp.get('trade_date')} 與資料包 {pack.get('trade_date')} 不同")
    if not pack.get("account"):
        err.append("資料包沒有結算資料（account 為 null），不能推導委託")
        return report(err)

    # ID 連號與引用
    layers = [("sources", "source_id", "S"), ("observations", "obs_id", "O"),
              ("inferences", "inf_id", "I"), ("decisions", "decision_id", "D")]
    ids = {}
    for key, idk, pre in layers:
        got = [x[idk] for x in dp[key]]
        if got != [f"{pre}{i}" for i in range(1, len(got) + 1)]:
            err.append(f"{idk} 不是從 {pre}1 開始連號")
        ids[pre] = set(got)
    for o in dp["observations"]:
        err += [f"{o['obs_id']} 引用不存在的 {r}" for r in o["source_ref"] if r not in ids["S"]]
    err += [f"market_view 引用不存在的 {r}" for r in dp["market_view"]["basis_refs"] if r not in ids["O"]]
    for i in dp["inferences"]:
        err += [f"{i['inf_id']} 引用不存在的 {r}" for r in i["premise_refs"] if r not in ids["O"]]
    for d in dp["decisions"]:
        err += [f"{d['decision_id']} 引用不存在的 {r}" for r in d["inference_refs"] if r not in ids["I"]]
    for n in dp["no_trade_decisions"]:
        err += [f"續抱 {n['ticker']} 引用不存在的 {r}" for r in n["reason_refs"] if r not in ids["I"]]

    # 覆蓋：資料包每檔 shares>0 的持股，要在 decisions 或 no_trade 出現且只出現一次
    held = {h["code"]: int(h["shares"]) for h in pack.get("holdings") or [] if int(h["shares"]) > 0}
    seen = Counter([d["ticker"] for d in dp["decisions"]] + [n["ticker"] for n in dp["no_trade_decisions"]])
    err += [f"前日持股 {t} 沒有交代" for t in held if seen[t] == 0]
    err += [f"{t} 重複出現 {c} 次" for t, c in seen.items() if c > 1]

    # 官方公式重算委託
    nav = float(pack["account"]["nav_tradable"])
    expected = Counter()
    for d in dp["decisions"]:
        t = d["ticker"]
        if t not in close:
            err.append(f"{t} 沒有前日收盤價")
            continue
        tgt = math.floor(d["target_weight"] * nav / close[t] / LOT + 1e-9) * LOT
        delta = tgt - held.get(t, 0)
        if delta:
            expected[(t, "BUY" if delta > 0 else "SELL", abs(delta), d["decision_id"])] += 1
    given = Counter((o["ticker"], o["side"], o["shares"], o["decision_ref"]) for o in dp["orders"])
    err += [f"推導有、委託沒有：{x}" for x in (expected - given).elements()]
    err += [f"委託有、推導沒有：{x}" for x in (given - expected).elements()]
    err += [f"{o['ticker']} 股數 {o['shares']} 不是千股倍數" for o in dp["orders"] if o["shares"] % LOT]
    err += [f"{o['ticker']} 賣出 {o['shares']} 超過持股 {held.get(o['ticker'], 0)}"
            for o in dp["orders"] if o["side"] == "SELL" and o["shares"] > held.get(o["ticker"], 0)]

    # 部位宣告
    buy = sum(o["shares"] * close[o["ticker"]] for o in dp["orders"] if o["side"] == "BUY" and o["ticker"] in close)
    sell = sum(o["shares"] * close[o["ticker"]] for o in dp["orders"] if o["side"] == "SELL" and o["ticker"] in close)
    intent = dp["market_view"]["posture"]["net_exposure_intent"]
    if not {"increase": buy > sell, "reduce": sell > buy, "hold": abs(buy - sell) <= 0.02 * nav}[intent]:
        err.append(f"宣告 {intent}，但買 {buy:,.0f}／賣 {sell:,.0f}")
    cash = float(pack["account"]["cash"]) - buy * (1 + FEE) + sell * (1 - FEE - TAX)
    lo, hi = dp["market_view"]["posture"]["target_cash_pct_range"]
    if not lo <= cash / nav <= hi:
        err.append(f"預估現金比 {cash / nav:.2%} 不在宣告區間 [{lo:.2%}, {hi:.2%}]")
    if cash < 0:
        err.append(f"預估現金為負 {cash:,.0f}")

    # 委託後持股
    post = dict(held)
    for o in dp["orders"]:
        post[o["ticker"]] = post.get(o["ticker"], 0) + (o["shares"] if o["side"] == "BUY" else -o["shares"])
    post = {t: n for t, n in post.items() if n > 0}
    if not 20 <= len(post) <= 30:
        err.append(f"委託後持股 {len(post)} 檔")
    for o in dp["orders"]:
        t = o["ticker"]
        if o["side"] == "BUY" and t in close:
            w = post.get(t, 0) * close[t] / nav
            if w > (0.25 if t == "2330" else 0.10):
                err.append(f"{t} 加碼後權重 {w:.2%} 超限")
    ts_re = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?\+08:00$")
    for s in dp["sources"]:
        if not ts_re.match(s["content_as_of"]):
            err.append(f"{s['source_id']} content_as_of 格式錯誤")
    print(f"檢查 {path.name}：{len(dp['orders'])} 筆委託、{len(dp['decisions'])} 筆決策、"
          f"{len(dp['no_trade_decisions'])} 筆續抱；預估現金比 {cash / nav:.2%}")
    return report(err)


def report(err: list[str]) -> int:
    if err:
        print(f"❌ {len(err)} 個問題，不要上傳：")
        for e in err:
            print(f"  - {e}")
        return 1
    print("✅ 獨立檢查全部通過")
    return 0


if __name__ == "__main__":
    sys.exit(main())
