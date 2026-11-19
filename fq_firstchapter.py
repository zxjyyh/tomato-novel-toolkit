# -*- coding: utf-8 -*-
"""
补「第一章发布时间」—— 番茄没有单独的"上架时间"字段，
目录接口里每章带 firstPassTime，第一章的就是最早发布时间，可当上架时间用。

代价：每本一次请求，返回约 300KB（整本目录），全量 50 万本不现实。
所以支持按条件筛选，只补需要的书：

    python fq_firstchapter.py --limit 50
    python fq_firstchapter.py --min-read 10000 --limit 2000
    python fq_firstchapter.py --where "read_count_real>=50000"
"""
import argparse
import json
import os
import sqlite3
import time

from playwright.sync_api import sync_playwright

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("FQ_DB") or os.path.join(BASE, "fq_books.db")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--min-read", type=int, default=0)
    ap.add_argument("--where", default="")
    ap.add_argument("--proxy", default="")
    ap.add_argument("--delay", type=float, default=1.5)
    ap.add_argument("--order", default="COALESCE(read_count_real,0) DESC")
    args = ap.parse_args()

    conn = sqlite3.connect(DB, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cols = [r[1] for r in cur.execute("PRAGMA table_info(books)")]
    if "first_chapter_ts" not in cols:
        cur.execute("ALTER TABLE books ADD COLUMN first_chapter_ts INTEGER")
        conn.commit()

    conds = ["first_chapter_ts IS NULL", "COALESCE(book_id,'')<>''"]
    if args.min_read:
        conds.append(f"COALESCE(read_count_real,0) >= {args.min_read}")
    if args.where:
        conds.append(f"({args.where})")
    sql = (f"SELECT book_id, title FROM books WHERE {' AND '.join(conds)} "
           f"ORDER BY {args.order} LIMIT {args.limit}")

    todo = cur.execute(sql).fetchall()
    print(f"待补 {len(todo)} 本", flush=True)
    if not todo:
        return

    proxy = {"server": args.proxy} if args.proxy else None
    ok = fail = 0
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, args=["--no-sandbox"], proxy=proxy)
        pg = b.new_context(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")).new_page()
        pg.goto("https://fanqienovel.com/library", wait_until="commit", timeout=60000)
        time.sleep(3)

        for i, r in enumerate(todo, 1):
            bid = r["book_id"]
            try:
                raw = pg.evaluate(
                    """async (id) => { const x = await fetch('/api/reader/directory/detail?bookId=' + id);
                         return await x.text(); }""", bid)
                if not raw or len(raw) < 50:
                    print(f"[{i}] {bid} 空返回（可能限流），停", flush=True)
                    break
                vols = (json.loads(raw).get("data") or {}).get("chapterListWithVolume") or []
                if not vols:
                    cur.execute("UPDATE books SET first_chapter_ts=0 WHERE book_id=?", (bid,))
                    conn.commit()
                    fail += 1
                    continue
                first = vols[0][0] if isinstance(vols[0], list) else vols[0]
                ts = int(first.get("firstPassTime") or 0)
                cur.execute("UPDATE books SET first_chapter_ts=? WHERE book_id=?", (ts, bid))
                conn.commit()
                ok += 1
                if i % 10 == 0:
                    print(f"  {i}/{len(todo)} …{r['title'][:16]} {time.strftime('%Y-%m-%d', time.localtime(ts)) if ts else '-'}", flush=True)
            except Exception as e:
                fail += 1
                print(f"[{i}] {bid} 失败 {str(e)[:50]}", flush=True)
            time.sleep(args.delay)
        b.close()

    n = cur.execute("SELECT COUNT(*) FROM books WHERE COALESCE(first_chapter_ts,0)>0").fetchone()[0]
    print(f"完成：成功 {ok}，失败 {fail}，库内已有首章时间 {n} 本")


if __name__ == "__main__":
    main()
