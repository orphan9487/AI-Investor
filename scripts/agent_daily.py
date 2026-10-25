"""每日 Agent 流程的指令列入口。完整步驟見 docs/daily_agent_playbook.md。

  python scripts/agent_daily.py prepare                 # 更新資料 + 結算 + 產出 context
  python scripts/agent_daily.py prepare --no-update     # 不更新資料（測試用）
  python scripts/agent_daily.py finalize                # 對最近一次 prepare 的交易日產出交易書與報告
  python scripts/agent_daily.py status                  # 帳本與各交易日狀態
  python scripts/agent_daily.py reconcile holdings.csv --cash 12345678 --asof 2026-10-27
                                                        # 用平台公布的持股覆蓋帳本
  python scripts/agent_daily.py --dry-run prepare       # 演練：寫到 dryrun/，不影響正式紀錄
"""
from __future__ import annotations

import argparse
import json
import sys
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
warnings.filterwarnings("ignore", message="目前使用自行估算的暫定標的池")

import pandas as pd  # noqa: E402

from etf_agent.agent import pipeline  # noqa: E402


def latest_prepared() -> str:
    runs = sorted(p.name for p in pipeline.RUNS_DIR.iterdir() if (p / "context.json").exists())
    if not runs:
        sys.exit("還沒有任何 prepare 過的交易日")
    return runs[-1]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="改用 dryrun/ 底下的 runs 與帳本，不影響正式比賽紀錄")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--no-update", action="store_true")
    p.add_argument("--trade-date")
    f = sub.add_parser("finalize")
    f.add_argument("--trade-date")
    sub.add_parser("status")
    r = sub.add_parser("reconcile")
    r.add_argument("holdings_csv", help="欄位 stock_id, shares")
    r.add_argument("--cash", type=float, required=True)
    r.add_argument("--asof", required=True)
    args = ap.parse_args()
    if args.dry_run:
        pipeline.RUNS_DIR = ROOT / "dryrun" / "runs"
        pipeline.LEDGER_PATH = ROOT / "dryrun" / "state" / "ledger.json"
        print("[dry-run] 使用 dryrun/，不影響正式紀錄")

    if args.cmd == "prepare":
        d = pipeline.prepare(update=not args.no_update, trade_date=args.trade_date)
        ctx = json.loads((d / "context.json").read_text(encoding="utf-8"))
        print(f"交易日 {ctx['trade_date']}（資料截至 {ctx['data_asof']}）")
        print(f"淨值 {ctx['portfolio']['nav']:,.0f}，累積 {ctx['portfolio']['return_since_start']:+.2%}")
        if ctx["portfolio"]["settled_runs"]:
            print(f"已結算：{ctx['portfolio']['settled_runs']}")
        print(f"決策材料：{d / 'context.md'}")
        print(f"請寫入：{d / 'decision.json'}，再執行 finalize")

    elif args.cmd == "finalize":
        td = args.trade_date or latest_prepared()
        d = pipeline.finalize(td)
        st = json.loads((d / "status.json").read_text(encoding="utf-8"))
        print(f"交易日 {td}：{st['state']}，決策來源 {st['author']}，委託 {st['n_orders']} 筆")
        for prob in st.get("problems", []):
            print(f"  ❌ {prob}")
        print(f"交易書：{d / 'orders.csv'}")
        print(f"決策報告：{d / 'report.md'}")

    elif args.cmd == "status":
        led = pipeline.load_ledger()
        print(f"帳本截至 {led.asof}：現金 {led.cash:,.0f}，持股 {len(led.shares)} 檔")
        if pipeline.RUNS_DIR.exists():
            for p in sorted(pipeline.RUNS_DIR.iterdir()):
                st = json.loads((p / "status.json").read_text(encoding="utf-8")) if (p / "status.json").exists() else {}
                print(f"  {p.name}: {st.get('state', '?')} {st.get('author', '')} {st.get('n_orders', '')}")

    elif args.cmd == "reconcile":
        df = pd.read_csv(args.holdings_csv, dtype={"stock_id": str})
        led = pipeline.load_ledger()
        diff = led.reconcile(args.cash, dict(zip(df["stock_id"], df["shares"].astype(int))), args.asof,
                             note=f"from {args.holdings_csv}")
        led.save(pipeline.LEDGER_PATH)
        print(json.dumps(diff, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
