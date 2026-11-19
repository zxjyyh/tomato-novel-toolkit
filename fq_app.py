# -*- coding: utf-8 -*-
"""番茄全站书库 —— Web 应用（扫描 + 下载 + 入库 + 补全元数据）

四个模块彼此独立，各自一个子进程，互不影响：
  扫描   把番茄分类翻完，只写库
  下载   三种入口，统一下到 uploads/
  入库   核验 uploads，匹配成功才移入 books/ 并标记已下载
  元数据 补大类/标签/开书时间

启动：
    python fq_app.py
默认 http://127.0.0.1:18081

安全相关的两个环境变量：
    FQ_PANEL_HOST   监听地址，默认 127.0.0.1（只本机可访问）
    FQ_PANEL_TOKEN  设置后所有 /api 请求需带 X-FQ-Token 头（或 ?token=）
                    监听非回环地址时强烈建议设置，否则同网段任何人都能触发爬取
"""
import csv
import io
import os
import secrets
import sqlite3
import subprocess
import sys
import time

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from urllib.parse import quote

from fq_common import (BASE, DB, PLAN, build_slices, ensure_db, load_plan,
                       lock_held, read_json, write_json)

STATIC = os.path.join(BASE, "static")
LOGS = os.path.join(BASE, "logs")

HOST = os.environ.get("FQ_PANEL_HOST", "127.0.0.1").strip() or "127.0.0.1"
PORT = int(os.environ.get("FQ_PANEL_PORT", "18081"))
TOKEN = (os.environ.get("FQ_PANEL_TOKEN") or "").strip()

app = FastAPI(title="番茄全站书库")

JOBS = {
    "scan": {"p": None, "mode": "", "t0": 0, "log": ""},
    "download": {"p": None, "mode": "", "t0": 0, "log": ""},
    "ingest": {"p": None, "mode": "", "t0": 0, "log": ""},
    "meta": {"p": None, "mode": "", "t0": 0, "log": ""},
}
STATUS_FILES = {
    "download": os.path.join(BASE, "status_download.json"),
    "ingest": os.path.join(BASE, "status_ingest.json"),
    "meta": os.path.join(BASE, "status_meta.json"),
}
# 扫描进程（fq_enum2 / fq_incr）会占这个锁；面板靠它判断「是不是有外部进程在跑」
SCAN_LOCK = DB + ".scan.lock"


# --------------------------------------------------------------------------
# 鉴权
# --------------------------------------------------------------------------
@app.middleware("http")
async def require_token(request: Request, call_next):
    """设了 FQ_PANEL_TOKEN 就校验；没设则不拦（此时默认只监听回环地址）。"""
    if TOKEN and request.url.path.startswith("/api"):
        got = request.headers.get("x-fq-token") or request.query_params.get("token") or ""
        if not secrets.compare_digest(got, TOKEN):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await call_next(request)


# --------------------------------------------------------------------------
# 数据库
# --------------------------------------------------------------------------
DB_READY = True
try:
    os.makedirs(os.path.dirname(os.path.abspath(DB)) or ".", exist_ok=True)
    _c = ensure_db()
    _c.close()
except Exception as _e:       # 路径不可写等：让服务起来，把错误暴露在日志里
    DB_READY = False
    print(f"[fq_app] ⚠ 数据库初始化失败：{_e}", flush=True)


def db():
    """只读连接。库不存在时 ensure_db 已经在启动阶段建好了。"""
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)
    c.execute("PRAGMA busy_timeout=8000")
    c.row_factory = sqlite3.Row
    return c


def tail(path, n=40):
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 150000))
            return f.read().decode("utf-8", "ignore").splitlines()[-n:]
    except Exception:
        return []


def read_status_file(name):
    p = STATUS_FILES.get(name)
    if not p:
        return {}
    st = read_json(p, {})
    return st if isinstance(st, dict) else {}


def job_running(name):
    j = JOBS.get(name)
    if not j:
        return False
    p = j["p"]
    return bool(p and p.poll() is None)


def scan_alive():
    """扫描可能是外部进程启动的（守护脚本 / 命令行 / 每日调度）。

    原先靠「数据库最近 180 秒有没有写断点」判断，但限流冷却时可能几分钟不写库，
    会误判成"已停止"；反过来进程刚被杀掉时又会误判成"还在跑"。
    现在改成探测跨进程锁：谁在跑谁就占着 .scan.lock，进程退出后锁由系统释放。
    """
    if job_running("scan"):
        return True
    return lock_held(SCAN_LOCK)


def job_state(name):
    j = JOBS.get(name) or {}
    run = job_running(name)
    if name == "scan" and not run:
        run = scan_alive()

    st = read_status_file(name)
    out = {k: v for k, v in st.items() if k != "log"}
    # 关键：running 以「进程是否还在」为准。
    # 子进程被 kill -9 / 容器重启时，status_*.json 里的 running:true 会残留，
    # 若直接 out.update(st) 覆盖，面板就会永久卡在"运行中"，开始按钮永久禁用。
    out.update(
        name=name,
        running=run,
        mode=(j.get("mode") or st.get("mode") or "") if run else "",
        uptime_min=(int((time.time() - j.get("t0", 0)) / 60)
                    if (run and j.get("t0")) else 0),
    )
    out["log"] = st.get("log") or tail(j.get("log", ""), 40)
    return out


def reset_status(name):
    """清掉状态文件里残留的 running=true"""
    p = STATUS_FILES.get(name)
    if not p:
        return
    st = read_json(p, {})
    if isinstance(st, dict) and st.get("running"):
        st["running"] = False
        write_json(p, st)


def start_job(name, cmd, mode="", env=None):
    if job_running(name):
        return {"ok": False, "message": "已经在跑"}
    os.makedirs(LOGS, exist_ok=True)
    log_path = os.path.join(LOGS, f"{name}_{mode or 'run'}_{time.strftime('%Y%m%d_%H%M%S')}.log")
    try:
        f = open(log_path, "w", encoding="utf-8")
    except Exception as e:
        return {"ok": False, "message": f"无法写日志：{str(e)[:60]}"}
    try:
        f.write(f"===== {time.strftime('%Y-%m-%d %H:%M:%S')} {name}/{mode} =====\n")
        f.flush()
        e = dict(os.environ)
        if env:
            e.update(env)
        p = subprocess.Popen(cmd, cwd=BASE, stdout=f, stderr=subprocess.STDOUT, env=e)
    except Exception as e:
        try:
            f.close()
        except Exception:
            pass
        return {"ok": False, "message": f"启动失败：{str(e)[:60]}"}
    # Popen 已经把句柄复制给子进程，父进程必须关掉自己这份，否则每启动一次泄漏一个 fd
    try:
        f.close()
    except Exception:
        pass
    JOBS[name].update(p=p, mode=mode, t0=int(time.time()), log=log_path)
    return {"ok": True, "message": f"已启动 {name}/{mode}"}


def stop_job(name):
    j = JOBS.get(name) or {}
    p = j.get("p")
    if p and p.poll() is None:
        p.terminate()
        try:
            p.wait(timeout=15)
        except subprocess.TimeoutExpired:
            p.kill()
        reset_status(name)
        return {"ok": True, "message": "已停止"}

    if name == "scan" and scan_alive():
        return {"ok": False,
                "message": "扫描是外部进程启动的（守护脚本或命令行），无法从这里停止"}

    if (read_status_file(name) or {}).get("running"):
        reset_status(name)
        return {"ok": True, "message": "已清理残留状态（进程已不在）"}
    return {"ok": False, "message": "当前没有在跑"}


# --------------------------------------------------------------------------
# 统计 / 列表
# --------------------------------------------------------------------------
_STATS_CACHE = {"t": 0, "v": None}
_COUNT_CACHE = {}
_SLICES_TOTAL = {"t": 0, "v": 0}


def cached_total(key, fn):
    v = _COUNT_CACHE.get(key)
    if v and time.time() - v[1] < 30:
        return v[0]
    n = fn()
    _COUNT_CACHE[key] = (n, time.time())
    if len(_COUNT_CACHE) > 200:      # 别让筛选条件把内存吃穿
        _COUNT_CACHE.clear()
        _COUNT_CACHE[key] = (n, time.time())
    return n


def slices_total():
    """全量切片数，从 cats.json 推导。

    不能写死：不同用户分类数量不同，写死会让进度条永远算错。
    没有 cats.json 时退化成「已记录过的切片数」，至少不会显示 0/0。
    """
    if time.time() - _SLICES_TOTAL["t"] < 60 and _SLICES_TOTAL["v"]:
        return _SLICES_TOTAL["v"]
    n = 0
    rows = load_plan(PLAN)
    if rows:
        try:
            n = len(build_slices(rows))
        except Exception:
            n = 0
    if not n:
        try:
            c = db()
            n = c.execute("SELECT COUNT(*) FROM slice_progress").fetchone()[0]
            c.close()
        except Exception:
            n = 0
    n = max(n, 1)
    _SLICES_TOTAL.update(t=time.time(), v=n)
    return n


@app.get("/api/stats")
def stats():
    if time.time() - _STATS_CACHE["t"] < 8 and _STATS_CACHE["v"]:
        v = dict(_STATS_CACHE["v"])
        v.update(running=scan_alive())
        return v
    try:
        c = db()
        books = c.execute("SELECT COUNT(*) FROM books").fetchone()[0]
        done = c.execute("SELECT COUNT(*) FROM slice_progress WHERE done=1").fetchone()[0]
        today_new = c.execute("SELECT COUNT(*) FROM books WHERE last_chapter_ts>=?",
                              (int(time.time()) - 86400,)).fetchone()[0]
        last = c.execute("SELECT MAX(updated_at) FROM slice_progress").fetchone()[0] or 0
        dl = c.execute("SELECT COUNT(*) FROM books WHERE downloaded=1").fetchone()[0]
        meta = c.execute("SELECT COUNT(*) FROM books WHERE COALESCE(meta_fetched_at,0)>0").fetchone()[0]
        c.close()
    except Exception as e:
        return {"books": 0, "slices_done": 0, "slices_total": 1, "today_new": 0,
                "last_write": 0, "downloaded": 0, "meta_done": 0, "running": False,
                "error": str(e)[:80]}
    out = {"books": books, "slices_done": done, "slices_total": slices_total(),
           "today_new": today_new, "last_write": last, "downloaded": dl, "meta_done": meta,
           "running": scan_alive()}
    _STATS_CACHE.update(t=time.time(), v=dict(out))
    return out


@app.get("/api/jobs")
def all_jobs():
    return {n: job_state(n) for n in JOBS}


@app.get("/api/cats")
def cats():
    c = db()
    rows = c.execute(
        "SELECT category_name name, category_id cid, gender, COUNT(*) n FROM books "
        "WHERE COALESCE(category_name,'')<>'' GROUP BY category_id, category_name, gender "
        "ORDER BY n DESC").fetchall()
    c.close()
    return [dict(r) for r in rows]


def _filters(q, cat, min_read, min_word, dl, gender=None):
    where, args = ["1=1"], []
    if q:
        where.append("(title LIKE ? OR author LIKE ?)")
        args += [f"%{q}%", f"%{q}%"]
    if cat:
        where.append("category_name=?")
        args.append(cat)
    if gender in (0, 1):
        where.append("gender=?")
        args.append(gender)
    if min_read:
        where.append("COALESCE(read_count_real,0)>=?")
        args.append(min_read)
    if min_word:
        where.append("COALESCE(word_count_real,0)>=?")
        args.append(min_word)
    if dl == "1":
        where.append("downloaded=1")
    elif dl == "0":
        where.append("COALESCE(downloaded,0)=0")
    return " AND ".join(where), args


@app.get("/api/books")
def books(page: int = 1, size: int = 50, q: str = "", cat: str = "",
          min_read: int = 0, min_word: int = 0, order: str = "read",
          gender: int = -1, dl: str = ""):
    # 没这两个下限，size=-1 会让 SQLite 的 LIMIT 变成"不限"，一次性把 80 万行拖进内存
    page = max(1, int(page))
    size = max(1, min(int(size), 500))

    w, args = _filters(q, cat, min_read, min_word, dl, gender if gender in (0, 1) else None)
    order_sql = {
        "read": "read_count_real DESC",
        "word": "word_count_real DESC",
        "new": "last_chapter_ts DESC",
        "old": "last_chapter_ts ASC",
        "title": "title ASC",
    }.get(order, "read_count_real DESC")

    c = db()
    try:
        total = cached_total(w + str(args), lambda: c.execute(
            f"SELECT COUNT(*) FROM books WHERE {w}", args).fetchone()[0])
        rows = c.execute(
            f"SELECT book_id,title,author,gender,category_name,word_count_real,read_count_real,"
            f"last_chapter_ts,first_chapter_ts,top_category,tags,cover_url,"
            f"COALESCE(downloaded,0) downloaded FROM books WHERE {w} ORDER BY {order_sql}, book_id "
            f"LIMIT ? OFFSET ?", args + [size, (page - 1) * size]).fetchall()
    finally:
        c.close()
    return {"total": total, "page": page, "size": size, "items": [dict(r) for r in rows]}


@app.get("/api/export")
def export(q: str = "", cat: str = "", min_read: int = 0, min_word: int = 0, dl: str = ""):
    w, args = _filters(q, cat, min_read, min_word, dl)
    sql = (f"SELECT book_id,title,author,gender,category_name,top_category,tags,"
           f"word_count_real,read_count_real,first_chapter_ts,last_chapter_ts,abstract "
           f"FROM books WHERE {w} ORDER BY COALESCE(read_count_real,0) DESC")

    def d(ts):
        return time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else ""

    def row_of(r):
        wc = r["word_count_real"] or 0
        words = (f"{wc/10000:.2f}".rstrip("0").rstrip(".") + "万字"
                 if wc >= 10000 else (f"{wc}字" if wc else ""))
        return [r["title"], r["author"], r["category_name"], r["top_category"] or "",
                r["tags"] or "", d(r["first_chapter_ts"]), d(r["last_chapter_ts"]),
                "男频" if r["gender"] == 1 else ("女频" if r["gender"] == 0 else ""),
                words, r["read_count_real"] or 0,
                (r["abstract"] or "").replace("\t", " ").replace("\n", " "),
                f"https://fanqienovel.com/page/{r['book_id']}"]

    def gen():
        """分块流式输出：全表 80 万行一次性拼成字符串会占几百 MB 内存"""
        c = db()
        buf = io.StringIO()
        wr = csv.writer(buf, delimiter="\t")
        try:
            buf.write("\ufeff")
            wr.writerow(["书名", "作者", "分类", "大类", "标签", "开始连载", "完结日期",
                         "受众", "字数", "在读人数", "简介", "链接"])
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate(0)
            for r in c.execute(sql, args):
                wr.writerow(row_of(r))
                if buf.tell() > 64 * 1024:
                    yield buf.getvalue()
                    buf.seek(0)
                    buf.truncate(0)
            if buf.tell():
                yield buf.getvalue()
        finally:
            c.close()

    name = f"番茄书库_{time.strftime('%Y%m%d_%H%M')}.tsv"
    # HTTP 头只能是 latin-1，中文文件名必须用 RFC 5987 编码，否则 500
    head = (f"attachment; filename=library_{time.strftime('%Y%m%d_%H%M')}.tsv; "
            f"filename*=UTF-8''{quote(name)}")
    return StreamingResponse(gen(), media_type="text/tab-separated-values",
                             headers={"Content-Disposition": head})


# --------------------------------------------------------------------------
# 扫描
# --------------------------------------------------------------------------
@app.get("/api/scan")
def scan_status():
    return job_state("scan")


@app.post("/api/scan/start")
def scan_start(body: dict):
    mode = body.get("mode", "full")
    cmd = [sys.executable, "-u"]
    note = ""
    if mode == "full":
        if os.path.exists(PLAN):
            cmd += ["fq_enum2.py", "--plan", PLAN, "--hours", "24"]
        else:
            # 没有分类计划就直接全分类枚举（切片多、配额消耗大，但至少能跑起来）
            cmd += ["fq_enum2.py", "--all", "--hours", "24"]
            note = "（未找到 cats.json，改用全分类枚举；建议先跑 fq_cats.py）"
    elif mode == "single":
        cid = body.get("cid")
        if not cid:
            return {"ok": False, "message": "缺少分类 ID"}
        cmd += ["fq_enum2.py", "--cid", str(cid),
                "--gender", str(body.get("gender", 0)), "--hours", "12"]
    elif mode == "incr":
        cmd += ["fq_incr.py", "--days", str(body.get("days", 3)),
                "--pages", str(body.get("pages", 3))]
    else:
        return {"ok": False, "message": "未知模式"}

    proxy = os.environ.get("FQ_PROXY")
    if proxy:
        cmd += ["--proxies" if mode != "incr" else "--proxy", proxy]

    r = start_job("scan", cmd, mode)
    if r.get("ok") and note:
        r["message"] += note
    return r


@app.post("/api/scan/stop")
def scan_stop():
    return stop_job("scan")


# --------------------------------------------------------------------------
# 下载
# --------------------------------------------------------------------------
@app.get("/api/download")
def download_status():
    return job_state("download")


@app.post("/api/download/start")
def download_start(body: dict):
    mode = body.get("mode", "unfinished")
    cmd = [sys.executable, "-u", "fq_download.py", "--mode", mode]
    if mode == "unfinished":
        if body.get("min_read"):
            cmd += ["--min-read", str(int(body["min_read"]))]
        if body.get("min_word"):
            cmd += ["--min-word", str(int(body["min_word"]))]
        if body.get("cat"):
            cmd += ["--cat", str(body["cat"])]
        if body.get("limit"):
            cmd += ["--limit", str(int(body["limit"]))]
    elif mode == "tsv":
        f = body.get("file") or ""
        if not f:
            return {"ok": False, "message": "未指定清单文件"}
        cmd += ["--file", f]
    elif mode == "search":
        if not body.get("keyword"):
            return {"ok": False, "message": "未填写书名"}
        cmd += ["--keyword", str(body["keyword"])]
    else:
        return {"ok": False, "message": "未知模式"}
    return start_job("download", cmd, mode)


@app.post("/api/download/stop")
def download_stop():
    return stop_job("download")


# --------------------------------------------------------------------------
# 入库
# --------------------------------------------------------------------------
@app.get("/api/ingest")
def ingest_status():
    return job_state("ingest")


@app.post("/api/ingest/start")
def ingest_start(body: dict):
    cmd = [sys.executable, "-u", "fq_ingest.py"]
    if body.get("watch"):
        cmd += ["--watch", "--interval", str(int(body.get("interval", 60)))]
        mode = "watch"
    else:
        cmd += ["--once"]
        mode = "once"
    return start_job("ingest", cmd, mode)


@app.post("/api/ingest/stop")
def ingest_stop():
    return stop_job("ingest")


# --------------------------------------------------------------------------
# 元数据
# --------------------------------------------------------------------------
@app.get("/api/meta")
def meta_status():
    return job_state("meta")


@app.post("/api/meta/start")
def meta_start(body: dict):
    cmd = [sys.executable, "-u", "fq_meta.py"]
    if body.get("min_read"):
        cmd += ["--min-read", str(int(body["min_read"]))]
    if body.get("min_word"):
        cmd += ["--min-word", str(int(body["min_word"]))]
    if body.get("limit"):
        cmd += ["--limit", str(int(body["limit"]))]
    return start_job("meta", cmd, "fill")


@app.post("/api/meta/stop")
def meta_stop():
    return stop_job("meta")


# --------------------------------------------------------------------------
# 静态页
# --------------------------------------------------------------------------
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as f:
        return HTMLResponse(f.read(), headers={"Cache-Control": "no-store"})


if __name__ == "__main__":
    import uvicorn

    if HOST not in ("127.0.0.1", "localhost", "::1") and not TOKEN:
        print("=" * 68)
        print(f"⚠  正在监听 {HOST}:{PORT}，但没有设置 FQ_PANEL_TOKEN。")
        print("   同一网络的任何人都能启动爬取任务、导出全库数据。")
        print("   建议：export FQ_PANEL_TOKEN='你的密码'")
        print("=" * 68)
    uvicorn.run(app, host=HOST, port=PORT)
