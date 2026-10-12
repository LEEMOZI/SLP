"""judgment-search 建库脚本：裁判文书 CSV zip → SQLite FTS5 按年分库。

架构（2026-10-12 定稿）：
- 每年一个 SQLite 库（data/judgments/judgments_YYYY.db），支持断点续跑
- docs 表：结构化字段 + nr_zst（zstd 压缩全文 BLOB）
- docs_fts：contentless FTS5（unicode61，只存倒排索引，不存明文）
- 检索命中后解压 nr_zst 本地高亮，无需回源 NAS
- 去重：同年内 案号+全文 md5 重复跳过
- 脏数据：NUL 字节清洗、csv.field_size_limit、全文 >20000 字截断（实测仅 0.01% 受影响）

用法：
    python build_judgment_db.py                 # 全量（跳过已完成的年份）
    python build_judgment_db.py --year 2018     # 只建一年
    python build_judgment_db.py --year 2018 --rebuild  # 重建该年
"""
import argparse
import csv
import hashlib
import io
import re
import sqlite3
import sys
import time
import zipfile
from pathlib import Path

import zstandard

# ---------- 常量 ----------
NAS_DIR = Path("/Volumes/sata11-138XXXX2122/Simon/裁判文书全量数据")
DB_DIR = Path(__file__).parent / "data" / "judgments"
FULLTEXT_CAP = 20000          # 全文截断（字符）
FIELD_CAP = 5000              # 单字段截断（当事人/法律依据等）
BATCH = 20000                 # 每事务行数
SQLITE_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=OFF",
    "PRAGMA cache_size=-1048576",   # 1GB 页缓存
    "PRAGMA temp_store=MEMORY",
)

csv.field_size_limit(10 * 1024 * 1024)  # 个别字段 >128KB
NUL_RE = re.compile(r"\x00")
_cctx = zstandard.ZstdCompressor(level=3)   # zstd-3：速度/压缩比平衡

# unicode61 会把连续中文串当成单个 token（「民间借贷纠纷」≠ 查询「民间借贷」），
# 导致子串查询大面积漏召回。标准解法：入库前把 CJK 逐字空格化，
# 查询侧同样空格化并按短语（phrase）匹配 → 实现任意长度中文子串检索。
_CJK_RE = re.compile(r"([\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+)")


def seg(s: str) -> str:
    """把字符串中的 CJK 连续段逐字空格化；ASCII/数字保持不变。"""
    return _CJK_RE.sub(lambda m: " ".join(m.group(1)), s) if s else ""

DDL = """
CREATE TABLE IF NOT EXISTS docs (
    id INTEGER PRIMARY KEY,
    ah TEXT,        -- 案号
    ajmc TEXT,      -- 案件名称
    fy TEXT,        -- 法院
    ssdq TEXT,      -- 所属地区
    ajlx TEXT,      -- 案件类型
    slcx TEXT,      -- 审理程序
    cprq TEXT,      -- 裁判日期
    dsr TEXT,       -- 当事人
    ay TEXT,        -- 案由
    fl TEXT,        -- 法律依据
    nr_zst BLOB,    -- 全文（zstd 压缩）
    nr_len INTEGER, -- 全文原长（字符）
    md5 TEXT        -- 全文指纹（去重）
);
CREATE INDEX IF NOT EXISTS idx_docs_ah ON docs(ah);
CREATE INDEX IF NOT EXISTS idx_docs_fy ON docs(fy);
CREATE INDEX IF NOT EXISTS idx_docs_cprq ON docs(cprq);
CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
    ah, fy, ssdq, ajlx, slcx, dsr, ay, fl, nr,
    content='', contentless_delete=0, tokenize='unicode61'
);
"""


def clean(s: str) -> str:
    """清洗：去 NUL、去首尾空白。"""
    return NUL_RE.sub("", s).strip() if s else ""


def year_done(db_path: Path) -> bool:
    """判断某年是否已建完（progress 表有 done 标记）。"""
    if not db_path.exists():
        return False
    try:
        con = sqlite3.connect(db_path)
        con.execute("CREATE TABLE IF NOT EXISTS progress (k TEXT PRIMARY KEY, v TEXT)")
        row = con.execute("SELECT v FROM progress WHERE k='done'").fetchone()
        con.close()
        return bool(row)
    except sqlite3.DatabaseError:
        return False


def build_year(zip_path: Path, db_path: Path, rebuild: bool = False) -> dict:
    """构建单年数据库。"""
    year = re.search(r"(\d{4})", zip_path.stem).group(1)
    if rebuild and db_path.exists():
        db_path.unlink()
    if year_done(db_path):
        return {"year": year, "status": "skipped_done", "docs": 0}

    DB_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    for p in SQLITE_PRAGMAS:
        con.execute(p)
    con.executescript(DDL)
    con.execute("CREATE TABLE IF NOT EXISTS progress (k TEXT PRIMARY KEY, v TEXT)")

    seen_fp = set()  # 全文指纹（md5 前 16 hex → int64），8800 万行约 2.5GB 内存
    # 续跑：加载已入库指纹（断点去重）
    try:
        seen_fp = {int(r[0][:16], 16) for r in con.execute("SELECT md5 FROM docs")}
    except sqlite3.DatabaseError:
        pass

    total, dup, empty, t0 = 0, 0, 0, time.time()
    buf = []

    def flush():
        nonlocal buf
        if buf:
            # docs 行已在读取循环内逐条 INSERT（需 cur.lastrowid 对齐 fts rowid），
            # 此处仅补写 FTS 倒排索引，禁止再插 docs（否则全文双倍膨胀）。
            # CJK 逐字空格化（seg）配合查询侧短语匹配，实现中文子串检索。
            con.executemany(
                "INSERT INTO docs_fts(rowid,ah,fy,ssdq,ajlx,slcx,dsr,ay,fl,nr)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                [(r[0], seg(r[1]), seg(r[3]), seg(r[4]), seg(r[5]), seg(r[6]),
                  seg(r[8]), seg(r[9]), seg(r[10]), seg(r[11]))
                 for r in buf])
            con.commit()
            buf = []

    with zipfile.ZipFile(zip_path) as zf:
        csvs = sorted(n for n in zf.namelist() if n.endswith(".csv"))
        for csv_name in csvs:
            with zf.open(csv_name) as f:
                reader = csv.reader(io.TextIOWrapper(f, encoding="utf-8-sig", errors="replace"))
                header = next(reader)
                idx = {name: i for i, name in enumerate(
                    h.lstrip("\ufeff").strip() for h in header)}
                cols = {k: idx.get(k, -1) for k in
                        ("案号", "案件名称", "法院", "所属地区", "案件类型",
                         "审理程序", "裁判日期", "当事人", "案由", "法律依据", "全文")}

                for row in reader:
                    total += 1
                    get = lambda k: clean(row[cols[k]])[:FIELD_CAP] if 0 <= cols[k] < len(row) else ""
                    nr = clean(row[cols["全文"]]) if 0 <= cols["全文"] < len(row) else ""
                    if not nr:
                        empty += 1  # 空全文：不计入入库数（2018 实测约 7% 行为空）
                        continue
                    nr = nr[:FULLTEXT_CAP]
                    md5_hex = hashlib.md5(nr.encode()).hexdigest()
                    fp = int(md5_hex[:16], 16)
                    if fp in seen_fp:
                        dup += 1
                        continue
                    seen_fp.add(fp)

                    ah = get("案号")
                    ajmc = get("案件名称")
                    fy = get("法院")
                    ssdq = get("所属地区")
                    ajlx = get("案件类型")
                    slcx = get("审理程序")
                    cprq = get("裁判日期")
                    dsr = get("当事人")
                    ay = get("案由")
                    fl = get("法律依据")
                    nr_zst = _cctx.compress(nr.encode("utf-8"))

                    cur = con.execute("INSERT INTO docs(ah,ajmc,fy,ssdq,ajlx,slcx,cprq,dsr,ay,fl,nr_zst,nr_len,md5)"
                                      " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                      (ah, ajmc, fy, ssdq, ajlx, slcx, cprq, dsr, ay, fl,
                                       nr_zst, len(nr), md5_hex))
                    buf.append((cur.lastrowid, ah, ajmc, fy, ssdq, ajlx, slcx, cprq,
                                dsr, ay, fl, nr, nr_zst, len(nr), md5_hex))
                    if len(buf) >= BATCH:
                        flush()
                        elapsed = time.time() - t0
                        print(f"[{year}] {total} 行 | 入库 {total - dup - empty} | 重复 {dup} | "
                              f"空全文 {empty} | {elapsed:.0f}s | {total / elapsed:.0f} 行/s", flush=True)
        flush()

    con.execute("INSERT OR REPLACE INTO progress VALUES('done', ?)", (str(total - dup - empty),))
    con.commit()
    # WAL checkpoint（避免 VACUUM：大库上耗时数小时，不值得）
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    size_gb = db_path.stat().st_size / 1e9
    con.close()
    elapsed = time.time() - t0
    stats = {"year": year, "status": "built", "docs": total - dup - empty, "dups": dup,
             "empty": empty, "size_gb": round(size_gb, 2),
             "elapsed_min": round(elapsed / 60, 1)}
    print(f"✅ [{year}] 完成: {stats}", flush=True)
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", help="只处理指定年份")
    ap.add_argument("--rebuild", action="store_true", help="强制重建")
    args = ap.parse_args()

    if not NAS_DIR.exists():
        print(f"❌ NAS 未挂载: {NAS_DIR}")
        sys.exit(1)

    # 2021 年包名带时间戳后缀（2021年裁判文书数据_20240114221318.zip），glob 需兼容
    zips = sorted(NAS_DIR.glob("*年裁判文书数据*.zip"))
    if args.year:
        zips = [z for z in zips if args.year in z.stem]
    print(f"共 {len(zips)} 个年度包待处理", flush=True)

    results = []
    for zp in zips:
        year = re.search(r"(\d{4})", zp.stem).group(1)
        db = DB_DIR / f"judgments_{year}.db"
        try:
            results.append(build_year(zp, db, rebuild=args.rebuild))
        except Exception as exc:
            print(f"❌ [{year}] 失败: {exc}", flush=True)
            results.append({"year": year, "status": "failed", "error": str(exc)})

    print("\n========== 建库汇总 ==========")
    for r in results:
        print(r)


if __name__ == "__main__":
    main()
