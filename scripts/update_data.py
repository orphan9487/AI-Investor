"""下載／更新所有資料。

  python scripts/update_data.py                       # 標的池 + 價格 + 抽查
  python scripts/update_data.py --finmind all         # 再加 FinMind 法人、月營收、股利、本益比
  python scripts/update_data.py --finmind revenue,institutional
  python scripts/update_data.py --rebuild-universe    # 重新估算暫定標的池
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from etf_agent.data import store  # noqa: E402
from etf_agent.data.finmind import DATASETS, FinMindClient  # noqa: E402
from etf_agent.data.prices import check_against_official, update_prices  # noqa: E402
from etf_agent.data.universe import build_provisional_universe, load_universe  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2021-01-01", help="價格與 FinMind 資料起始日")
    ap.add_argument("--universe-asof", default="2026-07-31")
    ap.add_argument("--rebuild-universe", action="store_true")
    ap.add_argument("--skip-prices", action="store_true")
    ap.add_argument("--finmind", default="", help=f"逗號分隔，可選 {','.join(DATASETS)} 或 all")
    args = ap.parse_args()
    store.ensure_dirs()

    if args.rebuild_universe or not store.UNIVERSE_PROVISIONAL_PATH.exists():
        print(f"估算暫定標的池（{args.universe_asof}）…")
        uni = build_provisional_universe(args.universe_asof)
        print(f"  上市 {(uni['market'] == 'twse').sum()} 檔、上櫃 {(uni['market'] == 'tpex').sum()} 檔")

    uni = load_universe()
    ids = uni["stock_id"].tolist()
    print(f"標的池：{len(ids)} 檔（來源：{uni['source'].iloc[0]}）")

    if not args.skip_prices:
        print("下載日 K（yfinance）…")
        prices = update_prices(uni, args.start)
        print(f"  {prices['stock_id'].nunique()} 檔、{prices['date'].nunique()} 個交易日，"
              f"{prices['date'].min().date()} ~ {prices['date'].max().date()}")
        bad = check_against_official(prices)
        if not bad.empty:
            print(bad.to_string(index=False))

    keys = list(DATASETS) if args.finmind == "all" else [k for k in args.finmind.split(",") if k]
    if keys:
        client = FinMindClient()
        n_req = len(keys) * len(ids)
        print(f"FinMind：{keys}，約 {n_req} 次請求，每次間隔 {client.min_interval:.0f} 秒"
              f"（約 {n_req * client.min_interval / 60:.0f} 分鐘）"
              + ("" if client.token else "。設定 FINMIND_TOKEN 可以快一倍"))
        for k in keys:
            client.update_many(k, ids, args.start)


if __name__ == "__main__":
    main()
