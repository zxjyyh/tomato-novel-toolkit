# -*- coding: utf-8 -*-
"""
下载模块 —— 三种入口，统一下到 uploads/，等入库模块核验

三种方式：
  --mode unfinished   补全数据库里未下载的书（可按热度/分类筛）
  --mode tsv          导入 TSV 清单（读「链接」列里的书号）
  --mode search       手动搜索书名，取第一个结果下载

依赖番茄下载引擎（Web UI 模式，默认 http://127.0.0.1:18430）

用法：
  python fq_download.py --mode unfinished --min-read 5000 --limit 100
  python fq_download.py --mode tsv --file 清单.tsv
  python fq_download.py --mode search --keyword "十日终焉"

进度写到 status_download.json，网页轮询读它。
"""
import argparse
import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request

from fq_common import BASE, Status, ensure_db

ENGINE = os.environ.get("FQ_ENGINE") or "http://127.0.0.1:18430"
STATUS = os.environ.get("FQ_DL_STATUS") or os.path.join(BASE, "status_download.json")
ENGINE_PWD = os.environ.get("FQ_ENGINE_PWD") or ""

STOP = {"v": False}
st = Status(STATUS)

# 禁用系统代理：引擎在本地，走代理会 502
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def log(msg):
    st.log(msg)


def set_status(**kw):
    st.set(**kw)


def http(path, data=None, method=None, timeout=30):
    url = ENGINE.rstrip("/") + path
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method or ("POST" if data else "GET"),
                                 headers={"Content-Type": "application/json"})
    if ENGINE_PWD:
        req.add_header("x-tomato-password", ENGINE_PWD)
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8", "ignore") or "{}")
    except urllib.error.HTTPError as e:
        return e.code, {"error": f"HTTP {e.code}"}
    except Exception as e:
        return 0, {"error": str(e)[:80]}


def engine_ok():
    code, d = http("/api/status", timeout=8)
    return code == 200


def active_jobs():
    code, d = http("/api/jobs", timeout=15)
    if code != 200:
        return []
    return [j for j in (d.get("items") or []) if j.get("state") in ("queued", "running", "downloading")]


def submit(bid):
    """投递一本书，返回 job_id 或 None（引擎忙/报错）"""
    for _ in range(3):
        busy = active_jobs()
        if busy:
            j = busy[0]
            log(f"    引擎还在跑 {j.get('title') or j.get('book_id')}，等它结束")
            wait_job(j.get("id"), timeout=1800)
            continue
        code, d = http("/api/jobs", {"book_id": str(bid)})
        if code == 200 and d.get("id"):
            return d["id"]
        if code == 429:
            time.sleep(5)
            continue
        log(f"    投递失败 {bid}: {d.get('error') or code}")
        return None
    return None


def wait_job(jid, timeout=3600):
    """轮询任务直到结束，返回 (state, title, chapters)"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if STOP["v"]:
            http(f"/api/jobs/{jid}/cancel", {}, method="POST", timeout=10)
            return "cancelled", "", 0
        code, d = http("/api/jobs", timeout=15)
        if code == 200:
            for j in d.get("items") or []:
                if j.get("id") == jid:
                    st = j.get("state")
                    if st in ("done", "failed", "cancelled"):
                        return st, j.get("title") or "", (j.get("progress") or {}).get("saved_chapters") or 0
        time.sleep(3)
    return "timeout", "", 0


def ids_unfinished(c, args):
    where = ["COALESCE(downloaded,0)=0"]
    params = []
    if args.min_read:
        where.append("COALESCE(read_count_real,0)>=?")
        params.append(args.min_read)
    if args.min_word:
        where.append("COALESCE(word_count_real,0)>=?")
        params.append(args.min_word)
    if args.cat:
        where.append("category_name=?")
        params.append(args.cat)
    if args.gender is not None:
        where.append("gender=?")
        params.append(args.gender)
    sql = (f"SELECT book_id, title FROM books WHERE {' AND '.join(where)} "
           f"ORDER BY COALESCE(read_count_real,0) DESC, book_id")
    if args.limit:
        sql += f" LIMIT {args.limit}"
    return [dict(r) for r in c.execute(sql, params)]


def ids_from_tsv(c, path):
    """读 TSV，优先取「链接」列的书号；没有就用「书名+作者」查库"""
    import csv
    rows = []
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            with open(path, encoding=enc, newline="") as f:
                rows = list(csv.reader(f, delimiter="\t"))
            break
        except Exception:
            continue
    if not rows:
        return []
    head = [x.strip() for x in rows[0]]
    i_url = head.index("链接") if "链接" in head else -1
    i_title = head.index("书名") if "书名" in head else 0
    i_author = head.index("作者") if "作者" in head else -1

    out, seen = [], set()
    for r in rows[1:]:
        if not r or not any(x.strip() for x in r):
            continue
        bid = ""
        if i_url >= 0 and i_url < len(r):
            m = re.search(r"/page/(\d{6,})", r[i_url])
            if m:
                bid = m.group(1)
        if not bid and i_title < len(r):
            t = r[i_title].strip()
            a = r[i_author].strip() if 0 <= i_author < len(r) else ""
            row = c.execute("SELECT book_id FROM books WHERE title=? AND author=? LIMIT 1",
                            (t, a)).fetchone()
            if row:
                bid = str(row[0])
        if bid and bid not in seen:
            seen.add(bid)
            out.append({"book_id": bid, "title": r[i_title].strip() if i_title < len(r) else ""})
    return out


def ids_from_search(keyword):
    """搜索书名，取第一个结果的 book_id"""
    q = urllib.parse.quote(keyword)
    code, d = http(f"/api/search?q={q}", timeout=20)
    if code != 200:
        return []
    items = d.get("items") or []
    return [{"book_id": str(x.get("book_id")), "title": x.get("book_name") or keyword}
            for x in items[:1] if x.get("book_id")]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", required=True, choices=["unfinished", "tsv", "search"])
    ap.add_argument("--file", default="")
    ap.add_argument("--keyword", default="")
    ap.add_argument("--min-read", type=int, default=0)
    ap.add_argument("--min-word", type=int, default=0)
    ap.add_argument("--cat", default="")
    ap.add_argument("--gender", type=int, default=None)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    if not engine_ok():
        log(f"❌ 下载引擎连不上：{ENGINE}")
        set_status(running=False, error="引擎未启动")
        return

    c = ensure_db()
    c.row_factory = sqlite3.Row

    if args.mode == "unfinished":
        todo = ids_unfinished(c, args)
    elif args.mode == "tsv":
        if not args.file or not os.path.exists(args.file):
            log("❌ 清单文件不存在")
            set_status(running=False, error="文件不存在")
            return
        todo = ids_from_tsv(c, args.file)
    else:
        if not args.keyword:
            log("❌ 缺少关键词")
            set_status(running=False, error="缺少关键词")
            return
        todo = ids_from_search(args.keyword)

    log(f"待下载 {len(todo):,} 本｜模式 {args.mode}")
    set_status(running=True, mode=args.mode, total=len(todo), done=0, failed=0,
               current="", started_at=int(time.time()), log=st.read().get("log", []))

    ok = fail = 0
    for i, item in enumerate(todo, 1):
        if STOP["v"]:
            log("收到停止指令，收工")
            break
        bid = item["book_id"]
        title = item.get("title") or ""
        set_status(current=title or bid, done=i - 1, failed=fail)

        jid = submit(bid)
        if not jid:
            fail += 1
            continue

        state, real_title, chapters = wait_job(jid)
        if state == "done":
            ok += 1
            title = real_title or title
            log(f"  [{i}/{len(todo)}] ✅ {title[:26]}（{chapters} 章）")
            c.execute("UPDATE books SET status='downloaded', enqueued_at=?, finished_at=?, "
                      "note=COALESCE(NULLIF(?,''),note) WHERE book_id=?",
                      (int(time.time()), int(time.time()), f"已下载 {chapters} 章", bid))
            c.commit()
        elif state == "cancelled":
            log("已取消当前任务，收工")
            break
        else:
            fail += 1
            log(f"  [{i}/{len(todo)}] ❌ {title[:26]} → {state}")
            c.execute("UPDATE books SET status='failed', finished_at=?, note=? WHERE book_id=?",
                      (int(time.time()), f"下载{state}", bid))
            c.commit()

        set_status(done=i, failed=fail, ok=ok)
        time.sleep(1)

    log(f"完成：成功 {ok}，失败 {fail}")
    set_status(running=False, done=len(todo), failed=fail, ok=ok, current="")
    c.close()


if __name__ == "__main__":
    import signal

    def _stop(sig, frame):
        STOP["v"] = True
        print("收到停止信号…", flush=True)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    main()
