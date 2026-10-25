"""本機資料快取位置與讀寫工具。"""
from __future__ import annotations

import os
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = Path(os.environ.get("ETF_AGENT_DATA_DIR", PROJECT_ROOT / "data"))

PRICES_PATH = DATA_DIR / "prices.parquet"
BENCHMARK_PATH = DATA_DIR / "benchmark.parquet"
UNIVERSE_PROVISIONAL_PATH = DATA_DIR / "universe_provisional.csv"
UNIVERSE_OFFICIAL_PATH = DATA_DIR / "universe_official.csv"
STOCK_INFO_PATH = DATA_DIR / "stock_info.parquet"
FINMIND_DIR = DATA_DIR / "finmind"


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    FINMIND_DIR.mkdir(parents=True, exist_ok=True)


def write_parquet(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    df.to_parquet(tmp, index=False)
    tmp.replace(path)


def read_parquet(path: Path) -> pd.DataFrame | None:
    if not path.exists():
        return None
    return pd.read_parquet(path)
