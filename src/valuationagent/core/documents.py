"""Bounded local parsing. Blocks retain source locations for model citations."""

import csv
import hashlib
import io
import json
import re
import zipfile
from pathlib import Path

MAX_CHARS = 600_000
MAX_BLOCKS = 1200


def parse_document(meta: dict) -> tuple[list[dict], list[str]]:
    path = Path(meta["storage_path"])
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != meta["sha256"]:
        raise ValueError("文件内容发生变化，请重新上传。")
    blocks, warnings = [], []
    total = 0

    def add(text, location):
        nonlocal total
        text = str(text).strip()
        for start in range(0, len(text), 2000):
            chunk = text[start:start + 2000]
            if total + len(chunk) > MAX_CHARS or len(blocks) >= MAX_BLOCKS:
                if "解析达到大小上限；剩余内容尚未读取。" not in warnings:
                    warnings.append("解析达到大小上限；剩余内容尚未读取。")
                return False
            blocks.append({"block_id": f"{meta['file_id']}:{len(blocks) + 1}",
                           "file_id": meta["file_id"], "location": location, "text": chunk})
            total += len(chunk)
        return True

    suffix = path.suffix.lower()
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            raise ValueError("PDF 解析依赖未安装，请安装项目 documents 可选依赖。") from None
        reader = PdfReader(io.BytesIO(raw))
        if reader.is_encrypted:
            raise ValueError("PDF 已加密，请提供可读取的版本。")
        for page_no, page in enumerate(reader.pages, 1):
            if page_no > 400:
                warnings.append("仅读取前 400 页，请将其余相关页面单独上传。")
                break
            text = page.extract_text() or ""
            if not text.strip():
                warnings.append(f"第 {page_no} 页未提取到文字；扫描件 OCR 尚未接入。")
            if not add(text, {"page": page_no}):
                break
    elif suffix == ".xlsx":
        try:
            from openpyxl import load_workbook
        except ImportError:
            raise ValueError("Excel 解析依赖未安装，请安装项目 documents 可选依赖。") from None
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            if sum(i.file_size for i in archive.infolist()) > 150 * 1024 * 1024:
                raise ValueError("工作簿解压后过大，请拆分相关工作表。")
        book = load_workbook(io.BytesIO(raw), read_only=True, data_only=False, keep_links=False)
        try:
            for sheet in book.worksheets:
                if sheet.max_column and sheet.max_column > 100:
                    warnings.append(f"{sheet.title} 仅读取前 100 列。")
                for index, row in enumerate(sheet.iter_rows(max_col=min(sheet.max_column or 100, 100)), 1):
                    if index > 10000:
                        warnings.append(f"{sheet.title} 仅读取前 10000 行。")
                        break
                    cells = []
                    for cell in row:
                        if cell.value is not None:
                            value = "[公式，未求值]" if cell.data_type == "f" else str(cell.value)
                            cells.append(f"{cell.coordinate}: {value}")
                    if cells and not add(" | ".join(cells), {"sheet": sheet.title, "row": index}):
                        break
                if total >= MAX_CHARS or len(blocks) >= MAX_BLOCKS:
                    break
        finally:
            book.close()
    elif suffix in {".txt", ".md", ".csv", ".json"}:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                text = raw.decode("gb18030")
            except UnicodeDecodeError:
                raise ValueError("文本编码无法识别，请另存为 UTF-8。") from None
        if suffix == ".json":
            text = json.dumps(json.loads(text, parse_float=str), ensure_ascii=False, indent=2)
        if suffix == ".csv":
            for row_no, row in enumerate(csv.reader(io.StringIO(text)), 1):
                if not add(" | ".join(row), {"row": row_no}):
                    break
        else:
            for line, paragraph in enumerate(re.split(r"\n\s*\n", text), 1):
                if not add(paragraph, {"section": line}):
                    break
    else:
        raise ValueError("此格式尚不能解析；旧版 .xls 请另存为 .xlsx，或上传 PDF/CSV/TXT。")
    return blocks, warnings[:30]
