"""滾動視窗結果的顯著性檢定：超額報酬是否 > 0、各變體是否勝過基準。

  python scripts/significance.py output/windows_base.csv output/windows_a.csv output/windows_b.csv

第一個檔案是基準。視窗長 24 日、每 5 日一個會互相重疊，所以 t 檢定只取不重疊樣本（--stride，預設 5 個視窗），
bootstrap 用置中移動區塊保留相關。變體有多個時，配對差的 t 檢定 p 值另列 Bonferroni 校正。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

sys.stdout.reconfigure(encoding="utf-8")


def block_boot_p(x: np.ndarray, block: int, rng: np.random.Generator, n_boot: int = 20000) -> float:
    """置中移動區塊 bootstrap 的單尾 p 值（H0：平均 <= 0）。"""
    n = len(x)
    block = max(1, min(block, n))
    c = x - x.mean()
    starts = np.arange(n - block + 1)
    nb = int(np.ceil(n / block))
    m = np.empty(n_boot)
    for b in range(n_boot):
        m[b] = np.concatenate([c[i:i + block] for i in rng.choice(starts, nb)])[:n].mean()
    return float((m >= x.mean()).mean())


def test_mean(x, stride: int, rng: np.random.Generator) -> dict:
    x = np.asarray(x, float)
    xi = x[::stride]
    p_t = float(stats.ttest_1samp(xi, 0, alternative="greater").pvalue) if len(xi) > 2 else float("nan")
    return {"n": len(x), "n_indep": len(xi), "mean": float(x.mean()), "p_t": p_t, "p_boot": block_boot_p(x, stride, rng)}


def load(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    return df[df["bench_return"].abs() <= 0.4].set_index("start")  # 排除大盤資料異常的視窗


def line(name: str, r: dict, extra: str = "") -> str:
    return f"{name:28s} n={r['n']:3d} 獨立={r['n_indep']:3d} 平均={r['mean']:+.2%} p_t={r['p_t']:.3f} p_boot={r['p_boot']:.3f}{extra}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+", type=Path, help="run_backtest.py --windows 的輸出；第一個是基準")
    ap.add_argument("--stride", type=int, default=5, help="幾個視窗才不重疊，也是 bootstrap 區塊長度")
    ap.add_argument("--seed", type=int, default=20261028)
    args = ap.parse_args()
    rng = np.random.default_rng(args.seed)

    frames = {p.stem: load(p) for p in args.files}
    for name, df in frames.items():
        print(line(f"{name} 超額", test_mean(df["excess"].values, args.stride, rng)))
    base_name, *variants = frames
    for name in variants:
        diff = (frames[name]["excess"] - frames[base_name]["excess"]).dropna()
        r = test_mean(diff.values, args.stride, rng)
        print(line(f"{name} - {base_name}", r, f"（Bonferroni×{len(variants)} {min(1, r['p_t'] * len(variants)):.3f}）"))


if __name__ == "__main__":
    main()
