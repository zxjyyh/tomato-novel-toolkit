# -*- coding: utf-8 -*-
"""生成分类计划 cats.json

为什么需要单独一步：
  枚举器要知道「哪些分类书多、书多的要按字数档拆开」，否则每个小分类都查 6 个
  字数档，浪费配额。这些信息（每个分类的总量）只能从列表接口的 total_count 拿到，
  所以先跑一遍这个脚本，把结果落成 cats.json。

输出格式（每行一个分类）：
  [[gender, category_id, name, total], ...]

注意：total_count 会被服务端截断在 1 万，所以 10000 表示「大于等于 1 万」。

用法：
  python fq_cats.py                          # 直连，输出 cats.json
  python fq_cats.py --proxy http://127.0.0.1:7890
  python fq_cats.py --out data/cats.json --delay 1.5
"""
import argparse
import json
import os
import random
import sys
import time

from playwright.sync_api import sync_playwright

from fq_common import plan_path, write_json

LIB_URL = "https://fanqienovel.com/library"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

CAT_JS = """async (g) => {
    const x = await fetch('/api/author/book/category_list/v0/?gender=' + g);
    return await x.text();
}"""

LIST_JS = """async (q) => {
    const x = await fetch('/api/author/library/book_list/v0/?' + q);
    return await x.text();
}"""


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def query(gender, cid):
    return (f"page_count=1&page_index=1&gender={gender}&category_id={cid}"
            f"&creation_status=0&word_count=-1&book_type=-1&sort=0")


def fetch_total(pg, gender, cid, retries=3, delay=2.0):
    """返回该分类的总量；拿不到返回 None（不猜测）"""
    for i in range(retries):
        try:
            raw = pg.evaluate(LIST_JS, query(gender, cid))
        except Exception:
            raw = None
        if raw and len(raw) >= 50:
            try:
                data = json.loads(raw).get("data") or {}
                total = data.get("total_count")
                if total is not None:
                    return int(total)
            except Exception:
                pass
        time.sleep(delay * (i + 1))
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=plan_path(), help="输出路径（默认 ./cats.json）")
    ap.add_argument("--proxy", default=os.environ.get("FQ_PROXY", ""),
                    help="HTTP 代理，留空=直连")
    ap.add_argument("--delay", type=float, default=1.2, help="分类之间的间隔秒")
    ap.add_argument("--jitter", type=float, default=0.5, help="间隔抖动幅度（0~1）")
    args = ap.parse_args()

    proxy = {"server": args.proxy} if args.proxy else None
    log(f"出口：{args.proxy or '直连'}")

    rows = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"], proxy=proxy)
        page = browser.new_context(user_agent=UA).new_page()
        # fetch 用的是页面上下文，必须先落到同源页面才能拿到接口
        page.goto(LIB_URL, wait_until="commit", timeout=60000)
        time.sleep(3)

        cats = {}
        for g in (0, 1):
            raw = page.evaluate(CAT_JS, g)
            try:
                cats[g] = json.loads(raw)["data"]
            except Exception:
                log(f"❌ 拉取{'男' if g else '女'}频分类失败，接口返回：{str(raw)[:120]}")
                browser.close()
                return 1
        log(f"分类：女频 {len(cats[0])}，男频 {len(cats[1])}")

        total_cats = len(cats[0]) + len(cats[1])
        done = 0
        for g in (0, 1):
            for c in cats[g]:
                cid = int(c["category_id"])
                name = str(c.get("name") or "").strip()
                done += 1
                total = fetch_total(page, g, cid)
                if total is None:
                    log(f"  [{done}/{total_cats}] ⚠ {name} cid={cid} 拿不到总量（可能限流），先记 0")
                    total = 0
                rows.append([g, cid, name, total])
                log(f"  [{done}/{total_cats}] {'男' if g else '女'} {name} cid={cid} → {total:,} 本")
                time.sleep(args.delay * (1 + random.uniform(-args.jitter, args.jitter)))

        browser.close()

    if not rows:
        log("❌ 没拿到任何分类，不写文件")
        return 1

    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    if not write_json(args.out, rows, atomic=True):
        log(f"❌ 写入失败：{args.out}")
        return 1

    capped = sum(1 for r in rows if r[3] >= 10000)
    log(f"✅ 已写出 {len(rows)} 个分类 → {args.out}")
    log(f"   其中 {capped} 个达到 1 万上限（枚举时会按字数档拆开）")
    log("   下一步：python fq_enum2.py --plan cats.json --delay 2.2")
    return 0


if __name__ == "__main__":
    sys.exit(main())
