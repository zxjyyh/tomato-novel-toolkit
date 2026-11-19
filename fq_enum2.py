# -*- coding: utf-8 -*-
"""番茄书库枚举器 v2 —— 带风控自适应

风控规律（2026-11-11 实测）：
  · 336 次请求 / 20 分钟  → 触发限流（接口返回 200 + 空 body）
  · 159 次请求 / 8 分钟   → 未触发
  · 触发后约 61 分钟自动解除
  · 换出口 IP 立刻可用（代理实测有效）

因此策略不是"跑得快"，而是"跑得久"：
  1. 每个出口每小时有配额（默认 200 页），用完就歇到下个小时
  2. 连续 2 次空返回 → 判定该出口被限流，冷却 65 分钟，换下一个出口
  3. 所有出口都在冷却 → 睡到最早解封的那个
  4. 每翻一页就落盘断点，中断不丢进度

用法：
    python fq_enum2.py --all --proxies http://127.0.0.1:7890 --hours 1
    python fq_enum2.py --plan cats.json --proxies "http://a:1,http://b:2"
    python fq_enum2.py --gender 0 --cid 851 --word -1 --proxies ... --hours 0.5
"""
import argparse
import json
import os
import random
import sys
import time

from playwright.sync_api import sync_playwright

from fq_common import (DB, build_slices, decode, ensure_db, load_map, load_plan,
                       parse_count, slice_key, try_lock)

PAGE_SIZE = 18
MAX_PAGES = 900
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
LIB_URL = "https://fanqienovel.com/library"

# 有映射表就直接在入库时解码，省掉"跑完再翻译"这一步
_MAP = load_map()


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def now():
    return int(time.time())


class Proxy:
    """一个出口 IP 的状态机"""

    def __init__(self, server, quota_per_hour=200, cooldown_min=65):
        self.server = server
        self.cfg = {"server": server} if server else None
        self.quota = quota_per_hour
        self.cooldown = cooldown_min * 60
        self.hour_start = now()
        self.used = 0
        self.blocked_until = 0
        self.fails = 0

    def healthy(self):
        if now() < self.blocked_until:
            return False
        if now() - self.hour_start >= 3600:
            self.hour_start, self.used = now(), 0
        return self.used < self.quota

    def wait_seconds(self):
        """还要等多久才能用"""
        if now() < self.blocked_until:
            return self.blocked_until - now()
        if now() - self.hour_start >= 3600:
            return 0
        return 3600 - (now() - self.hour_start)

    def use(self):
        self.used += 1

    def ok(self):
        self.fails = 0

    def fail(self):
        self.fails += 1
        if self.fails >= 2:
            self.blocked_until = now() + self.cooldown
            return True
        return False

    def label(self):
        return self.server or "直连"


def qs(page_index, gender, cid, word):
    return (f"page_count={PAGE_SIZE}&page_index={page_index}&gender={gender}"
            f"&category_id={cid}&creation_status=0&word_count={word}"
            f"&book_type=-1&sort=0")


def fetch(pg, query):
    """返回 (book_list, total_count, has_more)；books 为 None 表示疑似限流"""
    try:
        raw = pg.evaluate(
            """async (q) => {
                const x = await fetch('/api/author/library/book_list/v0/?' + q);
                return await x.text();
            }""", query)
    except Exception:
        return None, None, None
    if not raw or len(raw) < 50:
        return None, None, None          # 空返回 = 疑似限流
    try:
        d = json.loads(raw)
        data = d.get("data") or {}
        return data.get("book_list") or [], data.get("total_count"), data.get("has_more")
    except Exception:
        return None, None, None


def fetch_categories(pg):
    """{0: [(cid, name)], 1: [...]}；失败返回 None"""
    cats = {}
    for g in (0, 1):
        try:
            raw = pg.evaluate(
                """async (g) => { const x = await fetch('/api/author/book/category_list/v0/?gender=' + g);
                     return await x.text(); }""", g)
            cats[g] = [(int(c["category_id"]), str(c.get("name") or ""))
                       for c in json.loads(raw)["data"]]
        except Exception as e:
            log(f"❌ 拉取分类失败（gender={g}）：{str(e)[:80]}")
            return None
    return cats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxies", default=os.environ.get("FQ_PROXY", ""),
                    help="逗号分隔；默认读环境变量 FQ_PROXY，留空=直连")
    ap.add_argument("--gender", type=int)
    ap.add_argument("--cid", type=int)
    ap.add_argument("--word", type=int, default=-1)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--quota", type=int, default=200, help="每出口每小时最多翻多少页")
    ap.add_argument("--delay", type=float, default=2.5, help="基准间隔秒")
    ap.add_argument("--jitter", type=float, default=0.6, help="间隔抖动幅度（0~1）")
    ap.add_argument("--hours", type=float, default=8, help="本次最多跑多少小时")
    ap.add_argument("--plan", default="", help="分类统计 json（cats.json），用 fq_cats.py 生成")
    # 多机协作用：把切片按区间分给不同机器，避免两台重复爬
    ap.add_argument("--slice-start", type=int, default=int(os.environ.get("FQ_SLICE_START") or 1),
                    help="从第几个切片开始（1 起，含）")
    ap.add_argument("--slice-end", type=int, default=int(os.environ.get("FQ_SLICE_END") or 0),
                    help="到第几个切片结束（含），0=到最后一个")
    args = ap.parse_args()

    if not (args.cid is not None or args.all or args.plan):
        ap.error("需指定 --cid / --all / --plan 之一")

    # 同一台机器只允许一个枚举进程，否则两个进程会重复抓同一批切片、互相覆盖断点
    lock, locked = try_lock(DB + ".scan.lock")
    if not locked:
        log(f"❌ 已有枚举进程在跑（锁文件 {DB}.scan.lock），本次退出")
        return 1

    conn = ensure_db()
    cur = conn.cursor()
    try:
        if args.cid is not None:
            g = args.gender if args.gender is not None else 0
            slices = [(g, args.cid, "指定分类", args.word)]
            total_all = 1
        elif args.plan:
            rows = load_plan(args.plan)
            if not rows:
                log(f"❌ 分类计划不存在或格式不对：{args.plan}")
                log("   先跑：python fq_cats.py        # 生成分类与总量统计")
                log("   或者用：python fq_enum2.py --all  # 不用计划，直接全分类")
                return 1
            slices = build_slices(rows)
            total_all = len(slices)
            log(f"计划：{total_all} 个切片（大分类拆字数档），共 {len(rows)} 个分类")
        else:
            # --all 需要先知道分类表，只能开浏览器拿
            with sync_playwright() as p:
                px0 = args.proxies.split(",")[0].strip() if args.proxies.strip() else ""
                br = p.chromium.launch(headless=True, args=["--no-sandbox"],
                                       proxy={"server": px0} if px0 else None)
                pg = br.new_context(user_agent=UA).new_page()
                pg.goto(LIB_URL, wait_until="commit", timeout=60000)
                time.sleep(3)
                cats = fetch_categories(pg)
                try:
                    br.close()
                except Exception:
                    pass
            if not cats:
                log("❌ 拿不到分类表，退出")
                return 1
            log(f"分类表：女频 {len(cats[0])}，男频 {len(cats[1])}")
            slices = [(g, cid, name, w) for g in (0, 1) for cid, name in cats[g]
                      for w in (-1, 0, 1, 2, 3, 4)]
            total_all = len(slices)

        # 按区间切分（多机协作）。切片已按书籍数降序排好，编号即全局序号
        s0 = max(1, args.slice_start)
        e0 = min(args.slice_end if args.slice_end > 0 else total_all, total_all)
        if s0 > e0:
            log(f"❌ 切片区间非法：{s0}-{e0}（全量 {total_all}）")
            return 1
        mine = slices[s0 - 1:e0]
        if (s0, e0) != (1, total_all):
            log(f"本机负责切片 {s0}-{e0}（全量 {total_all} 个），本次处理 {len(mine)} 个")

        pools = ([Proxy(x.strip(), args.quota) for x in args.proxies.split(",") if x.strip()]
                 or [Proxy("")])
        log(f"出口：{', '.join(x.label() for x in pools)} | 配额 {args.quota} 页/小时/出口")

        deadline = time.time() + args.hours * 3600
        total_new = 0

        with sync_playwright() as p:
            br = p.chromium.launch(headless=True, args=["--no-sandbox"], proxy=pools[0].cfg)
            ctx = br.new_context(user_agent=UA)
            pg = ctx.new_page()
            pg.goto(LIB_URL, wait_until="commit", timeout=60000)
            time.sleep(3)
            cur_proxy_server = pools[0].server

            for si, (g, cid, cname, w) in enumerate(mine, s0):
                if time.time() > deadline:
                    log("到达时间上限，收工")
                    break
                key = slice_key(g, cid, w)
                row = cur.execute(
                    "SELECT next_page, done, total_got FROM slice_progress WHERE slice_key=?",
                    (key,)).fetchone()
                if row and row[1]:
                    continue
                page = (row[0] if row else 1) or 1
                got = (row[2] if row else 0) or 0
                cur.execute(
                    "INSERT INTO slice_progress(slice_key,gender,cat_id,cat_name,word,"
                    "next_page,total_got,updated_at) VALUES (?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(slice_key) DO UPDATE SET updated_at=excluded.updated_at",
                    (key, g, str(cid), cname, w, page, got, now()))
                conn.commit()

                log(f"[{si}/{total_all}] {'男' if g else '女'} {cname} cid={cid} "
                    f"word={w} 从第 {page} 页续")
                first_id, slice_done = None, False
                empty_retry, last_total = 0, 0

                while page <= MAX_PAGES and time.time() <= deadline:
                    usable = [px for px in pools if px.healthy()]
                    if not usable:
                        wait = min(px.wait_seconds() for px in pools)
                        # 不超过本次剩余时间，避免睡过 deadline 还多跑一轮
                        wait = max(1, min(wait, 900, int(deadline - time.time()) or 1))
                        log(f"    ⏸ 所有出口都不可用，等 {max(1, wait // 60)} 分钟")
                        time.sleep(wait)
                        continue

                    px = random.choice(usable)
                    # 只有换出口时才重建浏览器，否则复用当前会话（省掉每页 2 秒启动开销）
                    if cur_proxy_server != px.server:
                        try:
                            br.close()
                        except Exception:
                            pass
                        br = p.chromium.launch(headless=True, args=["--no-sandbox"], proxy=px.cfg)
                        ctx = br.new_context(user_agent=UA)
                        pg = ctx.new_page()
                        pg.goto(LIB_URL, wait_until="commit", timeout=60000)
                        time.sleep(2)
                        cur_proxy_server = px.server

                    px.use()
                    books, total, has_more = fetch(pg, qs(page, g, cid, w))
                    if books is None:
                        if px.fail():
                            log(f"    ⚠ {px.label()} 连续失败，冷却 65 分钟")
                        else:
                            time.sleep(5)
                        continue
                    px.ok()
                    last_total = total or last_total

                    if not books:
                        # 空页不一定是"到底了"：限流也会返回空数组。
                        # 连续重试 3 次仍空，才认定窗口结束，避免把限流误判成完成。
                        empty_retry += 1
                        if empty_retry <= 3:
                            time.sleep(4 * empty_retry)
                            continue
                        slice_done = True
                        break
                    empty_retry = 0

                    ids, seen = [], set()
                    for b in books:
                        bid = str(b.get("book_id") or "")
                        if bid and bid not in seen:
                            seen.add(bid)
                            ids.append(bid)
                    if not ids:
                        empty_retry += 1
                        if empty_retry <= 3:
                            time.sleep(4 * empty_retry)
                            continue
                        slice_done = True
                        break
                    if page == 1:
                        first_id = ids[0]
                    elif ids[0] == first_id:
                        slice_done = True
                        break

                    # 「新增」= 本页书号里库中尚不存在的数量。
                    # 不能用 cur.rowcount：ON CONFLICT DO UPDATE 命中已有行时它仍返回 1，
                    # 会把重复翻到的书每次都算成新增。
                    marks = ",".join("?" * len(ids))
                    existing = cur.execute(
                        f"SELECT COUNT(*) FROM books WHERE book_id IN ({marks})", ids
                    ).fetchone()[0]
                    new = len(ids) - existing

                    for b in books:
                        bid = str(b.get("book_id") or "")
                        if not bid:
                            continue
                        # 入库即解码：书名/作者/简介/字数/在读一次到位，
                        # 同时把混淆原文存进 *_raw，映射以后更新也能重刷。
                        title_raw = b.get("book_name") or ""
                        author_raw = b.get("author") or ""
                        ab_raw = (b.get("abstract") or "")[:2000]
                        w_raw = str(b.get("word_count") or "")
                        r_raw = str(b.get("read_count") or "")
                        cur.execute(
                            "INSERT INTO books(book_id,title,author,gender,category_id,"
                            "category_name,word_count,create_status,last_chapter_ts,source_slice,"
                            "abstract_raw,read_raw,word_raw,cover_url,status_raw,"
                            "title_raw,author_raw,abstract,word_count_real,read_count_real)"
                            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                            " ON CONFLICT(book_id) DO UPDATE SET"
                            "   abstract_raw=COALESCE(NULLIF(excluded.abstract_raw,''), abstract_raw),"
                            "   read_raw=COALESCE(NULLIF(excluded.read_raw,''), read_raw),"
                            "   word_raw=COALESCE(NULLIF(excluded.word_raw,''), word_raw),"
                            "   cover_url=COALESCE(NULLIF(excluded.cover_url,''), cover_url),"
                            "   title=CASE WHEN excluded.title<>'' THEN excluded.title ELSE title END,"
                            "   author=CASE WHEN excluded.author<>'' THEN excluded.author ELSE author END,"
                            "   abstract=CASE WHEN excluded.abstract<>'' THEN excluded.abstract ELSE abstract END,"
                            "   word_count_real=COALESCE(excluded.word_count_real, word_count_real),"
                            "   read_count_real=COALESCE(excluded.read_count_real, read_count_real)",
                            (bid, decode(title_raw, _MAP), decode(author_raw, _MAP), g, str(cid),
                             cname, b.get("word_number") or 0, b.get("creation_status") or 0,
                             int(b.get("last_chapter_time") or 0), key,
                             ab_raw, r_raw, w_raw,
                             b.get("thumb_url") or "",
                             str(b.get("status") or ""),
                             title_raw, author_raw, decode(ab_raw, _MAP),
                             parse_count(decode(w_raw, _MAP), ("字",)),
                             parse_count(decode(r_raw, _MAP), ("人在读", "在读", "人"))))
                    got += new
                    total_new += new

                    page += 1
                    cur.execute(
                        "UPDATE slice_progress SET next_page=?, total_got=?, updated_at=? "
                        "WHERE slice_key=?", (page, got, now(), key))
                    conn.commit()

                    if page % 20 == 0:
                        log(f"    …第 {page} 页，本切片 {got} 本（本页新增 {new}）")
                    if has_more is False:
                        slice_done = True
                        break
                    time.sleep(args.delay * (1 + random.uniform(-args.jitter, args.jitter)))

                # 完成度自检：接口声明 1 万本，实际只拿到几百本，说明是被限流中断的，
                # 不能标成完成，否则这个分类永远补不回来。
                # 阈值必须按真实页大小算（PAGE_SIZE=18），用 10 会偏严 44%，
                # 让正常抓完的切片也被误判成"限流中断"而反复重跑。
                if slice_done:
                    cap = last_total or 0
                    pages_done = page - 1
                    if cap and pages_done * PAGE_SIZE < cap * 0.5:
                        slice_done = False
                        log(f"    ⚠ 只翻到 {pages_done} 页（接口称 {cap} 本），"
                            f"疑似限流中断，不标记完成")

                if slice_done:
                    cur.execute("UPDATE slice_progress SET done=1, updated_at=? WHERE slice_key=?",
                                (now(), key))
                    conn.commit()
                    log(f"    ✅ 切片完成：{page - 1} 页，{got} 本")

            n = conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]
            log(f"本次新增 {total_new} 本，库内共 {n} 本")
            try:
                br.close()
            except Exception:
                pass
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
