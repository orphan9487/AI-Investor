"""主辦方每日資料包（DataPack，schema 3.x）：平台結算後的真實部位與合規狀態。

取得方式：參賽者後台「文件繳交」→「下載資料包」，放進 inbox/。
有資料包時，帳本一律以平台數字為準；模擬結算只在拿不到資料包時當備援。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .ledger import Ledger


class DataPackError(ValueError):
    pass


@dataclass
class DataPack:
    trade_date: str
    as_of_date: str | None
    generated_at: str
    team_id: str
    schema_version: str
    missing: list[str]
    prev_outcome: str
    void_reasons: list[str]
    excluded_codes: list[str]
    account: dict | None
    holdings: list[dict] | None
    compliance: dict
    source: str = ""
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def has_positions(self) -> bool:
        """account／holdings 是否為真值（null 代表平台還沒有結算資料，不能當成空手）。"""
        return self.account is not None and self.holdings is not None

    @property
    def violations(self) -> int:
        return int(self.compliance.get("violations", 0))


VOID_REASON_TEXT = {
    "insufficient_funds": "資金不足",
    "oversell": "賣出股數超過持股",
    "no_price": "標的缺當日成交均價且不在標的池內",
    "compliance_fail": "合規檢查未通過（持股檔數／標的池／現金部位／加碼個股權重）",
    "dplan_reference_chain": "D-Plan 文件引用鏈不完整",
    "dplan_decision_coverage": "D-Plan 決策覆蓋不完整",
    "dplan_round_lot": "委託非整股",
}


def load(path: Path) -> DataPack:
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    if d.get("doc_type") != "DataPack":
        raise DataPackError(f"{path} 不是 DataPack（doc_type={d.get('doc_type')!r}）")
    if not str(d.get("schema_version", "")).startswith("3."):
        raise DataPackError(f"不支援的 schema_version {d.get('schema_version')!r}（程式依 3.x 撰寫）")
    pdo = d.get("prev_day_orders") or {}
    return DataPack(
        trade_date=d["trade_date"],
        as_of_date=d.get("as_of_date"),
        generated_at=d.get("generated_at", ""),
        team_id=d.get("team_id", ""),
        schema_version=d["schema_version"],
        missing=list(d.get("missing") or []),
        prev_outcome=pdo.get("outcome", "unknown"),
        void_reasons=list(pdo.get("void_reasons") or []),
        excluded_codes=[str(c) for c in pdo.get("excluded_codes") or []],
        account=d.get("account"),
        holdings=d.get("holdings"),
        compliance=d.get("compliance") or {},
        source=str(path),
        raw=d,
    )


def find_latest(inbox: Path, trade_date: str | None = None) -> DataPack | None:
    """inbox 裡最新產生的 DataPack；給 trade_date 時只找對應那一天的。"""
    packs = []
    for p in sorted(inbox.glob("*.json")) if inbox.exists() else []:
        try:
            dp = load(p)
        except (DataPackError, json.JSONDecodeError, KeyError):
            continue
        if trade_date is None or dp.trade_date == trade_date:
            packs.append(dp)
    return max(packs, key=lambda x: x.generated_at) if packs else None


def apply_to_ledger(dp: DataPack, ledger: Ledger) -> dict:
    """用平台數字覆蓋帳本，回傳與原帳本的差異。沒有結算資料（null）時不動帳本。"""
    if not dp.has_positions:
        return {}
    shares = {str(h["code"]): int(h["shares"]) for h in dp.holdings if int(h.get("shares", 0)) > 0}
    diff = ledger.reconcile(float(dp.account["cash"]), shares, dp.as_of_date or ledger.asof or "",
                            note=f"DataPack {dp.generated_at}")
    ledger.accrued_cash = float(dp.account.get("receivable_cash", 0.0))
    ledger.accrued_shares = {str(h["code"]): int(h["receivable_shares"])
                             for h in dp.holdings if int(h.get("receivable_shares", 0)) > 0}
    if dp.as_of_date is None:
        ledger.asof = None  # 第一個交易日：全現金
    return diff


def summary_lines(dp: DataPack) -> list[str]:
    """給 context／報告用的重點摘要。"""
    lines = [f"資料包產生時間 {dp.generated_at}，部位截至 {dp.as_of_date or '（尚無結算）'}"]
    if dp.missing:
        lines.append(f"⚠️ 平台缺少資料：{', '.join(dp.missing)}")
    outcome = {"none": "前一交易日未繳交", "unknown": "前一交易日委託結果尚未判定",
               "voided": "❌ 前一交易日委託整批作廢", "executed": "前一交易日委託已成交"}.get(dp.prev_outcome, dp.prev_outcome)
    lines.append(outcome)
    if dp.void_reasons:
        lines.append("作廢原因：" + "；".join(VOID_REASON_TEXT.get(r, r) for r in dp.void_reasons))
    if dp.excluded_codes:
        lines.append(f"未成交（缺成交均價）：{', '.join(dp.excluded_codes)}")
    lines.append(f"累計違規 {dp.violations} 天（3 天取消資格）、異常提示 {dp.compliance.get('notices', 0)} 筆")
    for inc in dp.compliance.get("incidents", [])[-10:]:
        mark = "❌" if inc.get("status") == "fail" else "⚠️"
        lines.append(f"{mark} {inc.get('date')} {inc.get('check_code')}：{inc.get('message')}")
    return lines
