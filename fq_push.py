# -*- coding: utf-8 -*-
"""番茄投递器 —— 把枚举出来的 book_id 一本本喂给下载引擎

为什么串行：
  下载引擎（Tomato-Novel-Downloader Web UI）限制"同时只允许 1 个活跃任务"，
  第二个请求直接返回 429。所以必须投一本、等它 done、再投下一本。

用法：
    python fq_push.py --server http://127.0.0.1:18430 --limit 20
    python fq_push.py --server http://127.0.0.1:18430 --loop        # 一直跑到没书
    python fq_push.py --retry-failed                                 # 重投失败的

注意：本脚本只投递，不做入库。「是否已进书库」的权威字段是 books.downloaded，
由 fq_ingest.py 写入；这里的 status 只用于跟踪投递进度。
"""
import argparse
import json
import os
import sqlite3
import time
import urllib.error
import urllib.request

from fq_common import ensure_db

STATE_DONE = ("done", "success", "finished")
STATE_FAIL = ("failed", "error")
STATE_CANCEL = ("canceled", "cancelled")


def api(server, path, data=None, timeout=30):
    url = server.rstrip("/") + path
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(
        url, data=body, method="POST" if data is not None else "GET",
        headers={"Content-Type": "application/json"})
    pwd = os.environ.get("FQ_ENGINE_PWD")
    if pwd:
        req.add_header("x-tomato-password", pwd)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def wait_done(server, job_id, timeout_min=30, log=print):
    """轮询任务直到结束。引擎对取消状态的拼写有 canceled / cancelled 两种，都认。"""
    t0 = time.time()
    while time.time() - t0 < timeout_min * 60:
        time.sleep(5)
        try:
            items = api(server, "/api/jobs").get("items") or []
        except Exception:
            continue
        me = [i for i in items if i.get("id") == job_id]
        if not me:
            time.sleep(5)
            continue
        st = me[0].get("state")
        if st in STATE_DONE or st in STATE_FAIL or st in STATE_CANCEL:
            return st, me[0]
    return "timeout", None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default=os.environ.get("FQ_ENGINE") or "http://127.0.0.1:18430")
    ap.add_argument("--limit", type=int, default=10, help="本次投多少本")
    ap.add_argument("--loop", action="store_true", help="跑到没有新书为止")
    ap.add_argument("--retry-failed", action="store_true")
    ap.add_argument("--timeout-min", type=int, default=30)
    args = ap.parse_args()

    conn = ensure_db()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    def log(msg):
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

    try:
        if args.retry_failed:
            cur.execute("UPDATE books SET status='new' WHERE status='failed'")
            conn.commit()
            log("已把失败任务重置为待投递")

        ok = fail = 0
        n = 0
        while True:
            row = cur.execute(
                "SELECT book_id, title, category_name FROM books WHERE status='new' "
                "AND COALESCE(book_id,'')<>'' ORDER BY last_chapter_ts DESC LIMIT 1").fetchone()
            if not row:
                log("没有待投递的书了")
                break
            n += 1
            if not args.loop and n > args.limit:
                break

            bid = row["book_id"]
            try:
                r = api(args.server, "/api/jobs", {"book_id": bid})
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    log("引擎忙（429），等 20 秒")
                    time.sleep(20)
                    n -= 1
                    continue
                cur.execute("UPDATE books SET status='failed', note=? WHERE book_id=?",
                            (f"http {e.code}", bid))
                conn.commit()
                fail += 1
                continue
            except Exception as e:
                log(f"连不上引擎：{str(e)[:60]}")
                break

            job_id = r.get("id")
            log(f"[{n}] 投递 {bid} → job#{job_id}")
            st, info = wait_done(args.server, job_id, args.timeout_min, log)
            if st in STATE_DONE:
                cur.execute("UPDATE books SET status='downloaded', finished_at=?, note=NULL "
                            "WHERE book_id=?", (int(time.time()), bid))
                ok += 1
                log(f"    ✅ {(info or {}).get('title') or bid}")
            else:
                msg = (info or {}).get("message") or st
                cur.execute("UPDATE books SET status='failed', finished_at=?, note=? "
                            "WHERE book_id=?", (int(time.time()), str(msg)[:120], bid))
                fail += 1
                log(f"    ❌ {st} {str(msg)[:60]}")
            conn.commit()

        log(f"结束：成功 {ok}，失败 {fail}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    main()
