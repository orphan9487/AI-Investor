"""產出「ETF 投資策略說明文件」PDF（初賽前五日～初賽首日繳交）。

內容由 AI Agent（Claude）撰寫在 docs/strategy_statement.json，本程式只負責：
檢查官方字數限制 → 填入主辦單位範本 templates/etf_strategy_template.docx → 用 Word 匯出 PDF。

  python scripts/strategy_document.py
輸出：submissions/ETF投資策略說明文件.docx / .pdf
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import docx

ROOT = Path(__file__).resolve().parents[1]
sys.stdout.reconfigure(encoding="utf-8")

STATEMENT = ROOT / "docs" / "strategy_statement.json"
TEMPLATE = ROOT / "templates" / "etf_strategy_template.docx"
OUT_DIR = ROOT / "submissions"


def validate(d: dict) -> list[str]:
    errs = []
    name, theme, phil = d["etf_name"], d["theme"], d["philosophy"]
    if len(name) > 20:
        errs.append(f"ETF 名稱 {len(name)} 字，超過 20 字")
    if not name.startswith("主動"):
        errs.append("ETF 名稱開頭必須是「主動」")
    if len(theme) > 50:
        errs.append(f"投資主題 {len(theme)} 字，超過 50 字")
    if not 100 <= len(phil) <= 300:
        errs.append(f"投資理念 {len(phil)} 字，須介於 100–300 字")
    return errs


def fill(d: dict, out_docx: Path) -> None:
    doc = docx.Document(str(TEMPLATE))
    table = doc.tables[0]
    for row, key in zip(table.rows, ("etf_name", "theme", "philosophy")):
        cell = row.cells[1]
        cell.paragraphs[0].text = d[key]
    doc.save(str(out_docx))


def to_pdf(src: Path, dst: Path) -> None:
    import win32com.client  # Windows + Microsoft Word

    word = win32com.client.DispatchEx("Word.Application")
    word.Visible = False
    try:
        doc = word.Documents.Open(str(src.resolve()), ReadOnly=True)
        doc.SaveAs(str(dst.resolve()), FileFormat=17)  # 17 = wdFormatPDF
        doc.Close(False)
    finally:
        word.Quit()


def main() -> None:
    d = json.loads(STATEMENT.read_text(encoding="utf-8"))
    errs = validate(d)
    if errs:
        sys.exit("❌ " + "；".join(errs))
    OUT_DIR.mkdir(exist_ok=True)
    out_docx = OUT_DIR / "ETF投資策略說明文件.docx"
    out_pdf = OUT_DIR / "ETF投資策略說明文件.pdf"
    try:
        fill(d, out_docx)
        to_pdf(out_docx, out_pdf)
    except (PermissionError, OSError) as e:
        # 舊檔被開著（例如 Word 或 PDF 閱讀器）：改用帶時間的新檔名，不去關使用者的程式
        stamp = __import__("datetime").datetime.now().strftime("%Y%m%d_%H%M")
        out_docx = OUT_DIR / f"ETF投資策略說明文件_{stamp}.docx"
        out_pdf = OUT_DIR / f"ETF投資策略說明文件_{stamp}.pdf"
        print(f"原檔被其他程式開啟（{type(e).__name__}），改存新檔名")
        fill(d, out_docx)
        to_pdf(out_docx, out_pdf)
    print(f"名稱 {len(d['etf_name'])} 字／主題 {len(d['theme'])} 字／理念 {len(d['philosophy'])} 字，皆符合規定")
    print(f"PDF：{out_pdf}")


if __name__ == "__main__":
    main()
