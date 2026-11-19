# -*- coding: utf-8 -*-
"""番茄增量扫描 —— 每天只抓"新完结"的书

为什么需要单独一个：
  fq_enum2.py 是"全量枚举"，靠 slice_progress 记录哪些切片做完了，
  全跑完以后再跑就什么都不做（全部跳过）。
  新完结的书不会自己冒出来，必须用"按完结时间倒序"重新扫前几页。

原理：
  sort=1 = 按最后更新时间倒序（实测有效，第一页全是最近完结的）。
  每个分类翻前 N 页，遇到比 cutoff（默认 N 天前）更旧的就停。

用法：
    python fq_incr.py                 # 扫最近 7 天的新完本
    python fq_incr.py --days 3 --pages 3
"""
import argparse
import json
import os
import sys
import time

from playwright.sync_api import sync_playwright

from fq_common import DB, decode, ensure_db, load_map, parse_count, try_lock

PAGE_SIZE = 18
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
LIB_URL = "https://fanqienovel.com/library"

_MAP = load_map()


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def qs(page_index, gender, cid, word=-1, sort=1):
    return (f"page_count={PAGE_SIZE}&page_index={page_index}&gender={gender}"
            f"&category_id={cid}&creation_status=0&word_count={word}"
            f"&book_type=-1&sort={sort}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7, help="只抓最近 N 天完结的")
    ap.add_argument("--pages", type=int, default=5, help="每个分类最多翻几页")
    ap.add_argument("--max-cats", type=int, default=0, help="最多扫几个分类（调试用）")
    ap.add_argument("--proxy", default=os.environ.get("FQ_PROXY", ""))
    ap.add_argument("--delay", type=float, default=1.8)
    args = ap.parse_args()

    # 和全量枚举互斥：两个进程同时写 books 会重复抓取、互相覆盖断点
    lock, locked = try_lock(DB + ".scan.lock")
    if not locked:
        log(f"❌ 已有扫描进程在跑（锁文件 {DB}.scan.lock），本次退出")
        return 1

    cutoff = int(time.time()) - args.days * 86400
    conn = ensure_db()
    cur = conn.cursor()
    try:
        log(f"增量扫描：最近 {args.days} 天，每类最多 {args.pages} 页")
        proxy = {"server": args.proxy} if args.proxy else None

        with sync_playwright() as p:
            b = p.chromium.launch(headless=True, args=["--no-sandbox"], proxy=proxy)
            pg = b.new_context(user_agent=UA).new_page()
            pg.goto(LIB_URL, wait_until="commit", timeout=60000)
            time.sleep(3)

            cats = []
            for g in (0, 1):
                try:
                    raw = pg.evaluate(
                        """async (g) => { const x = await fetch('/api/author/book/category_list/v0/?gender=' + g);
                             return await x.text(); }""", g)
                    cats += [(g, int(c["category_id"]), str(c.get("name") or ""))
                             for c in json.loads(raw)["data"]]
                except Exception as e:
                    log(f"❌ 拉取分类失败（gender={g}）：{str(e)[:80]}")
                    b.close()
                    return 1
            if args.max_cats:
                cats = cats[:args.max_cats]
            log(f"分类 {len(cats)} 个")

            new_total = 0
            for i, (g, cid, name) in enumerate(cats, 1):
                got = 0
                for page in range(1, args.pages + 1):
                    try:
                        raw = pg.evaluate(
                            """async (q) => { const x = await fetch('/api/author/library/book_list/v0/?' + q);
                                 return await x.text(); }""", qs(page, g, cid))
                    except Exception:
                        break
                    if not raw or len(raw) < 50:
                        time.sleep(5)
                        break
                    try:
                        data = json.loads(raw).get("data") or {}
                    except Exception:
                        break
                    books = data.get("book_list") or []
                    if not books:
                        break

                    oldest = int(books[-1].get("last_chapter_time") or 0)
                    for bk in books:
                        ts = int(bk.get("last_chapter_time") or 0)
                        if ts < cutoff:
                            continue
                        bid = str(bk.get("book_id") or "")
                        if not bid:
                            continue
                        t_raw = bk.get("book_name") or ""
                        a_raw = bk.get("author") or ""
                        ab_raw = (bk.get("abstract") or "")[:2000]
                        w_raw = str(bk.get("word_count") or "")
                        r_raw = str(bk.get("read_count") or "")
                        # INSERT OR IGNORE 的 rowcount 可靠：插进去是 1，已存在是 0
                        cur.execute(
                            "INSERT OR IGNORE INTO books(book_id,title,author,gender,category_id,"
                            "category_name,create_status,last_chapter_ts,source_slice,abstract_raw,"
                            "read_raw,word_raw,cover_url,title_raw,author_raw,abstract,"
                            "word_count_real,read_count_real)"
                            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (bid, decode(t_raw, _MAP), decode(a_raw, _MAP), g, str(cid), name,
                             0, ts, "incr", ab_raw, r_raw, w_raw, bk.get("thumb_url") or "",
                             t_raw, a_raw, decode(ab_raw, _MAP),
                             parse_count(decode(w_raw, _MAP), ("字",)),
                             parse_count(decode(r_raw, _MAP), ("人在读", "在读", "人"))))
                        if cur.rowcount:
                            got += 1
                    conn.commit()
                    if oldest < cutoff:
                        break
                    time.sleep(args.delay)

                new_total += got
                if i % 20 == 0 or got:
                    log(f"  [{i}/{len(cats)}] {name}: 新增 {got}")

            try:
                b.close()
            except Exception:
                pass

        total = cur.execute("SELECT COUNT(*) FROM books").fetchone()[0]
        log(f"增量完成：本次新增 {new_total} 本，库内共 {total} 本")
        return 0
    finally:
        conn.close()
        if lock:
            try:
                lock.close()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
