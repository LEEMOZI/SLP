# -*- coding: utf-8 -*-
"""
core/office_reader.py 单元测试。

覆盖：
    [1] .docx  python-docx（段落 + 表格）
    [2] .doc   macOS textutil（非 darwin 跳过）
    [3] .pptx  python-pptx（文本框 + 演讲者备注）
    [4] .xlsx  openpyxl（多工作表网格）
    [5] 错标 .xls（内里真 xlsx）→ BytesIO 绕过扩展名嗅探
    [6] .xls   xlrd（旧版二进制，需 xlwt 生成夹具，缺则跳过）
    [7] 错标 .xlsx（内里真 xls OLE2）→ 魔数检测重定向 xlrd
    [8] 损坏 zip（.xls 名义 + xl/workbook.xml 存在）→ raw-strings 兜底 + 实体解码
    [9] .ppt   明确报错引导另存为 .pptx
    [10] 异常路径：不支持扩展名 / 文件不存在
    [11] 集成：contract_review.extract_text 走 Office 分支
    [12] 大文件截断防护：_MAX_CELLS / _MAX_SHAPES 上限（monkeypatch 缩小上限验证）
    [13] ingest 入库链路：scan_source 过滤 / read_document 分发 / read_office 清洗规则
         （子进程隔离：ingest.py 模块级有 argparse + config 副作用，且 import 较重）

用法：
    python tests/test_office_reader.py   # 退出码 0=全过 1=有失败
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.office_reader import extract_office_text  # noqa: E402

IS_DARWIN = sys.platform == "darwin"
HAS_XLWT = True
try:
    import xlwt  # noqa: F401
except ImportError:
    HAS_XLWT = False


# ============================================================
# 夹具生成
# ============================================================

def make_fixtures(tmp: str) -> dict:
    fx = {}

    # docx：段落 + 表格
    from docx import Document as Docx
    p = os.path.join(tmp, "t.docx")
    d = Docx()
    d.add_paragraph("测试合同")
    d.add_paragraph("甲方应于交付验收合格后三个工作日内支付合同总价款的百分之三十。")
    tb = d.add_table(rows=1, cols=2)
    tb.rows[0].cells[0].text = "条款"
    tb.rows[0].cells[1].text = "金额"
    d.save(p)
    fx["docx"] = p

    # doc：textutil 从 docx 转（仅 darwin）
    if IS_DARWIN and shutil.which("textutil"):
        import subprocess
        p_doc = os.path.join(tmp, "t.doc")
        subprocess.run(
            ["textutil", "-convert", "doc", p, "-output", p_doc],
            check=True, capture_output=True,
        )
        fx["doc"] = p_doc

    # pptx：文本框 + 备注
    from pptx import Presentation
    from pptx.util import Inches
    p = os.path.join(tmp, "t.pptx")
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(4), Inches(1))
    box.text_frame.text = "诉讼策略要点：管辖权异议应在答辩期内提出。"
    slide.notes_slide.notes_text_frame.text = "备注：准备好租赁合同原件。"
    prs.save(p)
    fx["pptx"] = p
    # 假 .ppt：占位文件（仅验证扩展名分支的报错引导，分支在读取内容前即抛出）
    p = os.path.join(tmp, "t.ppt")
    with open(p, "wb") as f:
        f.write(b"\xd0\xcf\x11\xe0placeholder")
    fx["ppt"] = p

    # xlsx：两个工作表
    from openpyxl import Workbook
    p = os.path.join(tmp, "t.xlsx")
    wb = Workbook()
    ws1 = wb.active
    ws1.title = "赔偿明细"
    ws1.append(("项目", "金额"))
    ws1.append(("医疗费", 50000))
    ws2 = wb.create_sheet("证据清单")
    ws2.append(("编号", "证据名称"))
    ws2.append(("1", "购销合同"))
    wb.save(p)
    fx["xlsx"] = p

    # 错标 .xls：openpyxl 写的真 xlsx，扩展名 .xls
    p = os.path.join(tmp, "t_fake.xls")
    wb2 = Workbook()
    ws = wb2.active
    ws.title = "证据清单"
    ws.append(("编号", "证据名称"))
    ws.append(("1", "购销合同"))
    wb2.save(p)
    fx["fake_xls"] = p

    # 真 .xls（xlwt）+ 反向错标 .xlsx
    if HAS_XLWT:
        p = os.path.join(tmp, "t_real.xls")
        wb3 = xlwt.Workbook()
        sh = wb3.add_sheet("旧版表格")
        sh.write(0, 0, "案号")
        sh.write(0, 1, "(2024)沪01民终1号")
        wb3.save(p)
        fx["real_xls"] = p
        fx["reverse_xlsx"] = os.path.join(tmp, "t_reverse.xlsx")
        shutil.copy(p, fx["reverse_xlsx"])

    # 损坏 zip：.xls 名义，含 xl/workbook.xml（XML 损坏）→ raw-strings 兜底
    # openpyxl 写入方会把非 ASCII 转成 &#NNNN; 数字实体，夹具模拟同样形态
    p = os.path.join(tmp, "t_broken.xls")
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("xl/workbook.xml", b"<workbook><sheets")  # 故意截断的畸形 XML
        zf.writestr(
            "xl/sharedStrings.xml",
            "<?xml version='1.0'?><sst><si><t>&#32534;&#21495;</t></si>"
            "<si><t>&#36141;&#38144;&#21512;&#21516;</t></si></sst>",
        )
        zf.writestr(
            "xl/worksheets/sheet1.xml",
            "<?xml version='1.0'?><worksheet><row><is><t>&#20184;&#27454;&#20973;&#35777;</t></is></row></worksheet>",
        )
    fx["broken_xls"] = p

    return fx


# ============================================================
# 测试
# ============================================================

def main() -> int:
    failures: list = []

    def check(label: str, cond: bool, detail: str = "") -> None:
        mark = "OK" if cond else "FAIL"
        print(f"   [{mark}] {label}" + (f" | {detail}" if detail and not cond else ""))
        if not cond:
            failures.append(label)

    with tempfile.TemporaryDirectory(prefix="office_ut_") as tmp:
        fx = make_fixtures(tmp)
        print(f"== [0] 夹具就绪（darwin={IS_DARWIN}, xlwt={HAS_XLWT}）==")

        print("== [1] docx 段落+表格 ==")
        text, meta = extract_office_text(fx["docx"])
        check("format=docx", meta["format"] == "docx", str(meta))
        check("含段落文本", "甲方应于交付验收合格后" in text)
        check("含表格单元格", "条款 | 金额" in text)
        check("tables 计数=1", meta.get("tables") == 1, str(meta))

        if IS_DARWIN and "doc" in fx:
            print("== [2] doc textutil ==")
            text, meta = extract_office_text(fx["doc"])
            check("format=doc(textutil)", meta["format"] == "doc(textutil)", str(meta))
            check("内容一致", "甲方应于交付验收合格后" in text)

        print("== [3] pptx 文本框+备注 ==")
        text, meta = extract_office_text(fx["pptx"])
        check("format=pptx", meta["format"] == "pptx", str(meta))
        check("幻灯片标记", "--- 幻灯片 1 ---" in text)
        check("文本框内容", "管辖权异议应在答辩期内提出" in text)
        check("备注内容", "租赁合同原件" in text)
        check("slides 计数=1", meta.get("slides") == 1, str(meta))

        print("== [4] xlsx 多工作表 ==")
        text, meta = extract_office_text(fx["xlsx"])
        check("format=xlsx", meta["format"] == "xlsx", str(meta))
        check("工作表1", "--- 工作表 赔偿明细 ---" in text and "医疗费 | 50000" in text)
        check("工作表2", "--- 工作表 证据清单 ---" in text and "购销合同" in text)
        check("sheets 计数=2", meta.get("sheets") == 2, str(meta))

        print("== [5] 错标 .xls（内里真 xlsx）→ BytesIO 重试 ==")
        text, meta = extract_office_text(fx["fake_xls"])
        check("结构化打开 format=xlsx", meta["format"] == "xlsx", str(meta))
        check("内容提取", "购销合同" in text)

        if HAS_XLWT and "real_xls" in fx:
            print("== [6] 真 .xls（xlrd）==")
            text, meta = extract_office_text(fx["real_xls"])
            check("format=xls", meta["format"] == "xls", str(meta))
            check("内容提取", "案号" in text and "(2024)沪01民终1号" in text)

            print("== [7] 错标 .xlsx（内里真 xls OLE2）→ xlrd 重定向 ==")
            text, meta = extract_office_text(fx["reverse_xlsx"])
            check("重定向 format=xls", meta["format"] == "xls", str(meta))
            check("内容提取", "(2024)沪01民终1号" in text)
        else:
            print("== [6]/[7] 跳过（未安装 xlwt，无法生成旧版二进制夹具）==")

        print("== [8] 损坏 zip → raw-strings 兜底 + 实体解码 ==")
        text, meta = extract_office_text(fx["broken_xls"])
        check("format=xlsx(raw-strings)", meta["format"] == "xlsx(raw-strings)", str(meta))
        check("数字实体已解码", "编号" in text and "购销合同" in text and "付款凭证" in text, text[:60])
        check("无残留实体", "&#32534;" not in text and "&#" not in text)

        print("== [9] .ppt 明确报错引导 ==")
        try:
            extract_office_text(fx["ppt"])
            check("应报 ValueError", False)
        except ValueError as exc:
            check("报错文案引导另存 .pptx", "另存为 .pptx" in str(exc), str(exc))

        print("== [10] 异常路径 ==")
        p_rtf = os.path.join(tmp, "x.rtf")
        with open(p_rtf, "wb") as f:
            f.write(b"dummy")
        try:
            extract_office_text(p_rtf)
            check("不支持扩展名应报 ValueError", False)
        except ValueError:
            check("不支持扩展名 ValueError", True)
        try:
            extract_office_text(os.path.join(tmp, "ghost.docx"))
            check("文件不存在应报 FileNotFoundError", False)
        except FileNotFoundError:
            check("文件不存在 FileNotFoundError", True)

        print("== [11] 集成：contract_review.extract_text Office 分支 ==")
        from core.contract_review import extract_text
        text, meta = extract_text(fx["docx"])
        check("docx meta.office", meta.get("office", {}).get("format") == "docx", str(meta))
        check("docx 文本透传", "甲方应于交付验收合格后" in text)
        text, meta = extract_text(fx["xlsx"])
        check("xlsx meta.office", meta.get("office", {}).get("format") == "xlsx", str(meta))
        check("xlsx 文本透传", "医疗费" in text)

        print("== [12] 大文件截断防护（monkeypatch 上限）==")
        import core.office_reader as _or
        from openpyxl import Workbook as _Wb

        # cells 上限：10 行 × 3 列 = 30 格，patch 到 10 → 访问 11 格后截断
        p_big = os.path.join(tmp, "big.xlsx")
        wb = _Wb()
        ws = wb.active
        ws.title = "大表"
        for r in range(1, 11):
            ws.append((f"r{r}c1", f"r{r}c2", f"r{r}c3"))
        wb.save(p_big)

        # shapes 上限：2 页 × 3 文本框 = 6，patch 到 3
        # （layout[6]=Blank 无占位符；layout[5] 会克隆标题占位符干扰计数）
        from pptx import Presentation as _Prs
        from pptx.util import Inches as _In
        p_many = os.path.join(tmp, "many.pptx")
        prs = _Prs()
        for s in (1, 2):
            sl = prs.slides.add_slide(prs.slide_layouts[6])
            for i in range(1, 4):
                bx = sl.shapes.add_textbox(_In(1), _In(i), _In(3), _In(0.5))
                bx.text_frame.text = f"S{s}P{i}"
        prs.save(p_many)

        orig_cells, orig_shapes = _or._MAX_CELLS, _or._MAX_SHAPES
        try:
            _or._MAX_CELLS = 10
            text, meta = extract_office_text(p_big)
            check("cells 计数封顶（上限+1=11）", meta.get("cells") == 11, str(meta))
            check("截断点前内容保留", "r3c3" in text and "r4c1" in text)
            check("截断点后内容丢弃", "r4c2" not in text and "r9c1" not in text)

            _or._MAX_SHAPES = 3
            text, meta = extract_office_text(p_many)
            check("shapes 计数封顶", meta.get("shapes") == 3, str(meta))
            check("首页形状保留", "S1P1" in text and "S1P3" in text)
            check("次页形状丢弃", "S2P1" not in text)
        finally:
            _or._MAX_CELLS, _or._MAX_SHAPES = orig_cells, orig_shapes

        print("== [13] ingest 入库链路（子进程隔离）==")
        ing_dir = os.path.join(tmp, "ing")
        os.makedirs(ing_dir)

        # 长文本 xlsx（清洗后 >50 字；含一行"目录"验证关键词过滤）
        p_ix = os.path.join(ing_dir, "ing.xlsx")
        wb = _Wb()
        ws = wb.active
        ws.append(("赔偿项目", "金额", "说明"))
        ws.append(("医疗费", 50000, "凭票据结算"))
        ws.append(("目录", "页码", "本行应被关键词清洗移除"))
        ws.append(("误工费", 12000, "按实际减少的收入计算，需提交单位出具的误工证明及工资流水"))
        wb.save(p_ix)

        # 短文本 xlsx（<50 字 → 清洗后为空 → []）
        p_short = os.path.join(ing_dir, "short.xlsx")
        wb = _Wb()
        ws = wb.active
        ws.append(("项目", "金额"))
        ws.append(("医疗费", 50000))
        wb.save(p_short)

        # 长文本 docx / txt
        from docx import Document as Docx
        p_id = os.path.join(ing_dir, "ing.docx")
        d = Docx()
        d.add_paragraph("测试合同（入库版）")
        d.add_paragraph("甲方应于交付验收合格后三个工作日内支付合同总价款的百分之三十，逾期按日万分之五支付违约金。")
        d.save(p_id)

        p_it = os.path.join(ing_dir, "ing.txt")
        with open(p_it, "w", encoding="utf-8") as f:
            f.write("纯文本合同内容，用于验证 read_text 分支的清洗与长度过滤逻辑均正常工作。"
                    "甲方应于交付验收合格后三个工作日内支付合同总价款的百分之三十。")

        # 损坏 .ppt 占位（read_office 吞异常 → []）
        p_bp = os.path.join(ing_dir, "broken.ppt")
        with open(p_bp, "wb") as f:
            f.write(b"\xd0\xcf\x11\xe0placeholder")

        # scan_source 目录：混合扩展名 + 子目录（只查过滤，不需真实内容）
        src = os.path.join(ing_dir, "src")
        os.makedirs(os.path.join(src, "sub"))
        for name in ("keep.docx", "keep2.xlsx", "keep3.xls", "keep4.pptx",
                     "keep5.pdf", "UPPER.TXT", "skip.rtf", "skip.exe"):
            with open(os.path.join(src, name), "wb") as f:
                f.write(b"x")
        with open(os.path.join(src, "sub", "nested.docx"), "wb") as f:
            f.write(b"x")

        code = f'''
import os, sys
sys.path.insert(0, {str(PROJECT_ROOT)!r})
import ingest  # 模块级 argparse/config 副作用隔离在本子进程

# read_office：长文本 xlsx（清洗 + 关键词行过滤）
pages = ingest.read_office({p_ix!r})
assert len(pages) == 1 and pages[0]["page"] is None, pages
t = pages[0]["text"]
assert "医疗费" in t and "误工费" in t, t[:100]
assert "目录" not in t, "关键词行未被清洗: " + t[:100]

# read_office：短文本 <50 字 → []
assert ingest.read_office({p_short!r}) == []

# read_office：损坏 .ppt → 异常吞掉 → []
assert ingest.read_office({p_bp!r}) == []

# read_document 按扩展名分发
assert "甲方应于交付验收合格后" in ingest.read_document({p_id!r})[0]["text"]
assert "甲方应于交付验收合格后" in ingest.read_document({p_it!r})[0]["text"]

# scan_source：扩展名白名单过滤 + 递归子目录 + 大写扩展名
files = sorted(os.path.basename(p) for p in ingest.scan_source({src!r}))
expect = sorted(["keep.docx", "keep2.xlsx", "keep3.xls", "keep4.pptx",
                 "keep5.pdf", "UPPER.TXT", "nested.docx"])
assert files == expect, files
print("INGEST-CHAIN-OK")
'''
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True,
        )
        check("子进程退出码=0", proc.returncode == 0,
              (proc.stderr or proc.stdout)[-400:])
        check("链路断言标记", "INGEST-CHAIN-OK" in proc.stdout)

    print()
    if failures:
        print(f"office_reader 单元测试 FAILED：{len(failures)} 项 → {failures}")
        return 1
    print("office_reader 单元测试 ALL PASS ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
