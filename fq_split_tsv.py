# -*- coding: utf-8 -*-
"""
把未下载书籍清单按固定行数切分为多个 TSV（每个文件都带表头，可独立导入）

排序：在读人数降序 → 字数降序 → book_id，保证前面的批次质量更高
"""
import argparse
import csv
import os
import sqlite3

from fq_common import (BASE, clean_field as clean, ensure_db, fmt_date,
                       fmt_word)

HEADER = ["书名", "作者", "分类", "大类", "标签", "开始连载",
          "完结日期", "受众", "字数", "在读人数", "简介", "链接"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per", type=int, default=5000, help="每个文件多少条")
    ap.add_argument("--outdir", default=os.path.join(BASE, "清单_切分"))
    ap.add_argument("--include-downloaded", action="store_true")
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)

    c = ensure_db()

    c.row_factory = sqlite3.Row

    where = "" if args.include_downloaded else " WHERE COALESCE(downloaded,0)=0"
    total = c.execute(f"SELECT COUNT(*) FROM books{where}").fetchone()[0]
    nfiles = (total + args.per - 1) // args.per
    print(f"待导出 {total:,} 本 -> {nfiles} 个文件（每 {args.per} 本）", flush=True)

    sql = (f"SELECT book_id,title,author,category_name,gender,word_count_real,"
           f"read_count_real,last_chapter_ts,first_chapter_ts,abstract FROM books{where} "
           f"ORDER BY COALESCE(read_count_real,0) DESC, COALESCE(word_count_real,0) DESC, book_id")
    cur = c.execute(sql)

    idx, written, in_file = 1, 0, 0
    f = None
    wr = None
    made = []

    def open_new():
        nonlocal f, wr, in_file
        path = os.path.join(args.outdir, f"完整清单_未下载_{idx:02d}.tsv")
        f = open(path, "w", encoding="utf-8-sig", newline="")
        wr = csv.writer(f, delimiter="\t", quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
        wr.writerow(HEADER)
        made.append(path)
        in_file = 0
        return path

    open_new()
    for r in cur:
        bid = str(r["book_id"] or "")
        row = [
            clean(r["title"]),
            clean(r["author"]),
            clean(r["category_name"]),
            "", "",
            fmt_date(r["first_chapter_ts"]),
            fmt_date(r["last_chapter_ts"]),
            ("女频" if r["gender"] == 0 else ("男频" if r["gender"] == 1 else "")),
            fmt_word(r["word_count_real"]),
            str(r["read_count_real"] or 0),
            clean(r["abstract"]),
            f"https://fanqienovel.com/page/{bid}" if bid else "",
        ]
        wr.writerow(row)
        written += 1
        in_file += 1
        if written % args.per == 0:
            f.close()
            print(f"  第 {idx:02d} 个完成（累计 {written:,}）", flush=True)
            idx += 1
            open_new()
    f.close()

    # 正好整除时最后会多出一个只有表头的空文件，删掉
    path = os.path.join(args.outdir, f"完整清单_未下载_{idx:02d}.tsv")
    if os.path.exists(path) and in_file == 0:
        os.remove(path)
        if path in made:
            made.remove(path)

    sizes = sum(os.path.getsize(p) for p in made) / 1024 / 1024
    print(f"\n完成：{written:,} 行，{len(made)} 个文件，共 {sizes:.1f} MB")
    print(f"目录：{args.outdir}")
    c.close()


if __name__ == "__main__":
    main()
