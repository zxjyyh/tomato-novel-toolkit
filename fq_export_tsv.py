# -*- coding: utf-8 -*-
"""
导出未下载书籍清单，格式对齐旧项目的「完整清单_完结清单_日期.tsv」

12 列：书名 作者 分类 大类 标签 开始连载 完结日期 受众 字数 在读人数 简介 链接
- 能填的：书名/作者/分类/完结日期/受众/字数/在读/简介/链接
- 填不了的：大类/标签（番茄列表接口不提供）、开始连载（仅少量书有）
"""
import argparse
import csv
import os
import sqlite3
import time

from fq_common import (BASE, clean_field as clean, ensure_db, fmt_date,
                       fmt_word)

HEADER = ["书名", "作者", "分类", "大类", "标签", "开始连载",
          "完结日期", "受众", "字数", "在读人数", "简介", "链接"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(BASE, f"完整清单_未下载_{time.strftime('%Y-%m-%d')}.tsv"))
    ap.add_argument("--include-downloaded", action="store_true", help="含已下载的书")
    ap.add_argument("--min-read", type=int, default=0, help="在读人数下限")
    ap.add_argument("--min-word", type=int, default=0, help="字数下限")
    ap.add_argument("--limit", type=int, default=0, help="最多导出多少条（0=不限）")
    args = ap.parse_args()

    c = ensure_db()
    c.row_factory = sqlite3.Row

    where = []
    params = []
    if not args.include_downloaded:
        where.append("COALESCE(downloaded,0)=0")
    if args.min_read:
        where.append("COALESCE(read_count_real,0)>=?")
        params.append(args.min_read)
    if args.min_word:
        where.append("COALESCE(word_count_real,0)>=?")
        params.append(args.min_word)
    w = (" WHERE " + " AND ".join(where)) if where else ""

    total = c.execute(f"SELECT COUNT(*) FROM books{w}", params).fetchone()[0]
    print(f"符合条件：{total:,} 本", flush=True)

    sql = (f"SELECT book_id,title,author,category_name,gender,word_count_real,"
           f"read_count_real,last_chapter_ts,first_chapter_ts,abstract FROM books{w} "
           f"ORDER BY COALESCE(read_count_real,0) DESC, COALESCE(word_count_real,0) DESC, book_id")
    if args.limit:
        sql += f" LIMIT {args.limit}"

    cur = c.execute(sql, params)
    n = 0
    empty_abs = 0
    # 必须用 csv.writer：简介里有英文双引号，裸拼字符串会让 csv 解析器把后续
    # 多条记录吞成一条（实测 1952 行被合并）。writer 会自动加引号并转义。
    with open(args.out, "w", encoding="utf-8-sig", newline="") as f:
        wr = csv.writer(f, delimiter="\t", quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
        wr.writerow(HEADER)
        for r in cur:
            bid = str(r["book_id"] or "")
            title = clean(r["title"])
            author = clean(r["author"])
            cat = clean(r["category_name"])
            gender = "女频" if r["gender"] == 0 else ("男频" if r["gender"] == 1 else "")
            words = fmt_word(r["word_count_real"])
            reads = str(r["read_count_real"] or 0)
            end = fmt_date(r["last_chapter_ts"])
            start = fmt_date(r["first_chapter_ts"])
            abstract = clean(r["abstract"])
            if not abstract:
                empty_abs += 1
            link = f"https://fanqienovel.com/page/{bid}" if bid else ""
            row = [title, author, cat, "", "", start, end, gender, words, reads, abstract, link]
            wr.writerow(row)
            n += 1
            if n % 20000 == 0:
                print(f"  已写 {n:,}", flush=True)

    size = os.path.getsize(args.out) / 1024 / 1024
    print(f"\n完成：{n:,} 行 -> {args.out}")
    print(f"文件大小：{size:.1f} MB")
    print(f"简介为空：{empty_abs:,} 本")
    c.close()


if __name__ == "__main__":
    main()
