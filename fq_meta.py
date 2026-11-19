# -*- coding: utf-8 -*-
"""补全元数据：从番茄书籍详情页一次性取回全部字段并回写数据库

一次请求能拿到：
  书名/作者（正确值，可修正列表接口的形近字污染）
  简介、字数、在读、完结状态
  大类（categoryV2 里 Dim=1）、标签（Dim=2 角色 + Dim=3 情节）
  开书时间（章节列表第一章的 firstPassTime）

用法：
  python fq_meta.py --limit 20 --dry-run          # 只抓 20 本看看，不写库
  python fq_meta.py --min-read 10000              # 补在读 1 万以上的
  python fq_meta.py --limit 500 --delay 1.2       # 小批量实测
  python fq_meta.py                               # 全量补（很慢，慎用）
"""
import argparse
import json
import os
import random
import sys
import time
import urllib.request

from fq_common import BASE, Status, ensure_db

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

MAX_FAIL = 8          # 连续失败多少次判定被限流
COOLDOWN = 300        # 冷却秒数

STATUS = os.environ.get("FQ_META_STATUS") or os.path.join(BASE, "status_meta.json")
st = Status(STATUS)

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def log(msg):
    st.log(msg)


def extract_page(html):
    """从 __INITIAL_STATE__ 里抠出 page 节点（含简介/分类/章节列表）"""
    i = html.find("window.__INITIAL_STATE__")
    if i < 0:
        return None
    j = html.find("{", i)
    if j < 0:
        return None
    depth = 0
    end = -1
    for k in range(j, len(html)):
        ch = html[k]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = k + 1
                break
    if end < 0:
        return None
    try:
        d = json.loads(html[j:end])
    except Exception:
        return None
    return d.get("page") or None


def parse_book(p):
    """把 page 节点转成要写库的字段"""
    if not p:
        return None
    bid = str(p.get("bookId") or "")
    if not bid:
        return None

    cats = []
    raw = p.get("categoryV2")
    if isinstance(raw, str) and raw.strip():
        try:
            cats = json.loads(raw)
        except Exception:
            cats = []
    elif isinstance(raw, list):
        cats = raw

    top, tags, main_cat = [], [], ""
    for c in cats:
        if not isinstance(c, dict):
            continue
        name = (c.get("Name") or "").strip()
        dim = c.get("Dim")
        if not name:
            continue
        if dim == 10 and not main_cat:
            main_cat = name            # 主分类（细分）
        elif dim == 1:
            top.append(name)           # 大类
        elif dim in (2, 3):
            tags.append(name)          # 角色 / 情节

    # 开书时间 = 第一章的 firstPassTime
    first_ts = 0
    vols = p.get("chapterListWithVolume") or []
    try:
        if vols and isinstance(vols[0], list) and vols[0]:
            first_ts = int(vols[0][0].get("firstPassTime") or 0)
    except Exception:
        first_ts = 0

    return {
        "book_id": bid,
        "title": (p.get("bookName") or "").strip(),
        "author": (p.get("authorName") or p.get("author") or "").strip(),
        "abstract": (p.get("abstract") or "").strip(),
        "word": int(p.get("wordNumber") or 0),
        "read": int(p.get("readCount") or 0),
        "last_ts": int(p.get("lastPublishTime") or 0),
        "first_ts": first_ts,
        "main_cat": main_cat,
        "top_category": "、".join(dict.fromkeys(top)),
        "tags": ",".join(dict.fromkeys(tags)),
        "status": int(p.get("creationStatus") if p.get("creationStatus") is not None else -1),
    }


def fetch(bid, timeout=25):
    url = f"https://fanqienovel.com/page/{bid}"
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    with _OPENER.open(req, timeout=timeout) as r:
        html = r.read().decode("utf-8", "ignore")
    return parse_book(extract_page(html))


UPDATE_SQL = """
UPDATE books SET
  title            = COALESCE(NULLIF(?,''), title),
  author           = COALESCE(NULLIF(?,''), author),
  abstract         = COALESCE(NULLIF(?,''), abstract),
  word_count_real  = CASE WHEN ? > 0 THEN ? ELSE word_count_real END,
  read_count_real  = CASE WHEN ? > 0 THEN ? ELSE read_count_real END,
  first_chapter_ts = COALESCE(?, first_chapter_ts),
  last_chapter_ts  = COALESCE(?, last_chapter_ts),
  top_category     = COALESCE(NULLIF(?,''), top_category),
  tags             = COALESCE(NULLIF(?,''), tags),
  category_name    = COALESCE(NULLIF(?,''), category_name),
  meta_fetched_at  = ?
WHERE book_id = ?
"""


def params_of(d):
    """按 UPDATE_SQL 的占位符顺序展开"""
    return (d["title"], d["author"], d["abstract"],
            d["word"], d["word"],
            d["read"], d["read"],
            d["first_ts"] or None, d["last_ts"] or None,
            d["top_category"], d["tags"], d["main_cat"],
            int(time.time()), d["book_id"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--min-read", type=int, default=0)
    ap.add_argument("--min-word", type=int, default=0)
    ap.add_argument("--where", default="")
    ap.add_argument("--delay", type=float, default=1.0, help="每本间隔秒")
    ap.add_argument("--force", action="store_true", help="重补已补过的书")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--proxy", default=os.environ.get("FQ_PROXY", ""),
                    help="留空=直连；传 http://ip:port 走代理")
    args = ap.parse_args()

    global _OPENER
    if args.proxy:
        _OPENER = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": args.proxy, "https": args.proxy}))
        log(f"走代理：{args.proxy}")
    else:
        log("直连模式")

    c = ensure_db()
    try:
        where, params = [], []
        if not args.force:
            where.append("COALESCE(meta_fetched_at,0)=0")
        if args.min_read:
            where.append("COALESCE(read_count_real,0)>=?")
            params.append(args.min_read)
        if args.min_word:
            where.append("COALESCE(word_count_real,0)>=?")
            params.append(args.min_word)
        if args.where:
            where.append(f"({args.where})")
        w = (" WHERE " + " AND ".join(where)) if where else ""

        total = c.execute(f"SELECT COUNT(*) FROM books{w}", params).fetchone()[0]
        sql = (f"SELECT book_id FROM books{w} "
               f"ORDER BY COALESCE(read_count_real,0) DESC, book_id")
        if args.limit:
            sql += f" LIMIT {args.limit}"
        ids = [r[0] for r in c.execute(sql, params)]
        log(f"待补全 {len(ids):,} 本（库内符合条件 {total:,}）")
        st.set(running=True, total=len(ids), done=0, ok=0, fail=0,
               started_at=int(time.time()))

        ok = fail = 0
        consecutive_fail = 0
        t0 = time.time()
        patch = []

        for i, bid in enumerate(ids, 1):
            if consecutive_fail >= MAX_FAIL:
                log(f"⚠ 连续失败 {MAX_FAIL} 次，疑似被限流，等 {COOLDOWN} 秒")
                time.sleep(COOLDOWN)
                consecutive_fail = 0

            try:
                d = fetch(bid)
                consecutive_fail = 0
            except Exception as e:
                fail += 1
                consecutive_fail += 1
                if i <= 5 or i % 50 == 0:
                    log(f"  失败 {bid}: {str(e)[:50]}")
                time.sleep(args.delay + random.uniform(0, 0.4))
                continue

            if not d:
                fail += 1
                consecutive_fail += 1
                time.sleep(args.delay)
                continue

            ok += 1
            if i <= 3 or i % 200 == 0:
                log(f"  {d['title'][:20]} | {d['top_category'] or '-'} | "
                    f"{d['word']}字 | {d['read']}读 | 开书 "
                    f"{time.strftime('%Y-%m-%d', time.localtime(d['first_ts'])) if d['first_ts'] else '-'}")

            if not args.dry_run:
                patch.append(params_of(d))
                if len(patch) >= 200:
                    c.executemany(UPDATE_SQL, patch)
                    c.commit()
                    patch.clear()

            st.set(done=i, ok=ok, fail=fail, current=d["title"][:30])
            if i % 50 == 0:
                el = time.time() - t0
                rate = i / el if el else 0
                left = (len(ids) - i) / rate if rate else 0
                log(f"  …… {i}/{len(ids)}，成功 {ok}，失败 {fail}，"
                    f"{rate:.1f} 本/秒，预计剩余 {left/60:.0f} 分钟")

            time.sleep(args.delay + random.uniform(0, 0.4))

        if patch and not args.dry_run:
            c.executemany(UPDATE_SQL, patch)
            c.commit()

        el = time.time() - t0
        log(f"完成：成功 {ok:,}，失败 {fail:,}，耗时 {el/60:.1f} 分钟")
        st.set(running=False, done=len(ids), ok=ok, fail=fail, finished_at=int(time.time()))
        if not args.dry_run:
            n = c.execute("SELECT COUNT(*) FROM books "
                          "WHERE COALESCE(meta_fetched_at,0)>0").fetchone()[0]
            f = c.execute("SELECT COUNT(*) FROM books "
                          "WHERE COALESCE(first_chapter_ts,0)>0").fetchone()[0]
            log(f"库内已补全 {n:,} 本，有开书时间 {f:,} 本")
        return 0
    finally:
        c.close()


if __name__ == "__main__":
    sys.exit(main())
