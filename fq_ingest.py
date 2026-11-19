# -*- coding: utf-8 -*-
"""扫描入库模块 —— 核验 uploads 里的 epub，匹配成功才移入 books/ 并标记已下载

流程：
  1. 文件已写完（大小在连续两轮检查间没变，且 mtime 至少 settle 秒前）
  2. 打开 zip，读出 dc:identifier（书号）和 dc:title（正确书名）
  3. 查库分流：
       读不出书号 / 打不开        → _broken/
       库内无此书                 → _unknown/
       库内已标记已下载           → _duplicate/
       匹配成功                   → books/{分类}/ ，标记 downloaded=1
  4. 顺手用 epub 里的正确书名修正列表接口的形近字污染

用法：
  python fq_ingest.py --once --dry-run       # 预演，不动文件
  python fq_ingest.py --once                 # 跑一轮
  python fq_ingest.py --watch                # 常驻，每 60 秒扫一次
"""
import argparse
import os
import shutil
import sqlite3
import sys
import time

from fq_common import (BASE, Status, ensure_db, read_epub_meta, same_enough,
                       sanitize)

UPLOADS = os.environ.get("FQ_UPLOADS") or os.path.join(BASE, "uploads")
BOOKS = os.environ.get("FQ_BOOKS") or os.path.join(BASE, "books")
STATUS = os.environ.get("FQ_INGEST_STATUS") or os.path.join(BASE, "status_ingest.json")

st = Status(STATUS)


def log(msg):
    st.log(msg)


def _drop(path, d, name):
    try:
        os.makedirs(d, exist_ok=True)
        tgt = os.path.join(d, name)
        if os.path.exists(tgt):
            tgt = os.path.join(d, f"{os.path.splitext(name)[0]}_{int(time.time())}.epub")
        shutil.move(path, tgt)
    except Exception:
        pass


# 上一轮看到的 (大小, mtime)：用来判断文件是否还在写
_SEEN = {}


def stable_files(root, settle):
    """找出已经写完的 epub

    判据是「大小在两次检查之间没变」+「mtime 至少 settle 秒前」。
    只看 mtime 不够：大文件写了几分钟，mtime 可能已经"够旧"，但字节还没写完。
    """
    now = time.time()
    out, alive = [], set()
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if not d.startswith("_")]
        for fn in fns:
            if not fn.lower().endswith(".epub"):
                continue
            p = os.path.join(dp, fn)
            try:
                s = os.stat(p)
            except OSError:
                continue
            if s.st_size <= 0:
                continue
            alive.add(p)
            prev = _SEEN.get(p)
            _SEEN[p] = (s.st_size, s.st_mtime)
            if now - s.st_mtime < settle:
                continue
            if prev is None or prev[0] == s.st_size:
                out.append(p)
    for gone in [p for p in _SEEN if p not in alive]:
        _SEEN.pop(gone, None)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--uploads", default=UPLOADS)
    ap.add_argument("--books", default=BOOKS)
    ap.add_argument("--min-cat", type=int, default=500)
    ap.add_argument("--settle", type=int, default=10, help="文件多久没变化才算写完")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--interval", type=int, default=60)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    os.makedirs(args.uploads, exist_ok=True)
    os.makedirs(args.books, exist_ok=True)

    c = ensure_db()
    c.row_factory = sqlite3.Row

    def run_once():
        # 每轮重算：常驻模式下库里会不断进新书，分类计数不能只取启动时的快照
        counts = {r[0]: r[1] for r in c.execute(
            "SELECT COALESCE(NULLIF(category_name,''),'(空)') cat, COUNT(*) FROM books GROUP BY 1")}

        def target_cat(name):
            nm = (name or "").strip() or "(空)"
            return "其他" if counts.get(nm, 0) < args.min_cat else nm

        files = stable_files(args.uploads, args.settle)
        st.set(running=True, total=len(files), done=0, moved=0, dup=0,
               unknown=0, broken=0, started_at=int(time.time()))
        log(f"检查 uploads：{len(files)} 个待处理文件")
        stat = {"moved": 0, "dup": 0, "unknown": 0, "broken": 0}

        for i, path in enumerate(files, 1):
            meta = read_epub_meta(path)
            base = os.path.basename(path)

            if not meta or not meta.get("book_id"):
                stat["broken"] += 1
                if not args.dry_run:
                    _drop(path, os.path.join(args.uploads, "_broken"), base)
                log(f"  [{i}] 损坏/无书号 → _broken/{base[:30]}")
                continue

            bid = meta["book_id"]
            row = c.execute("SELECT title, author, category_name, COALESCE(downloaded,0) dl, "
                            "COALESCE(file_rel,'') rel FROM books WHERE book_id=?", (bid,)).fetchone()

            if not row:
                stat["unknown"] += 1
                if not args.dry_run:
                    _drop(path, os.path.join(args.uploads, "_unknown"), base)
                log(f"  [{i}] 库内无此书 {bid} → _unknown/{base[:30]}")
                continue

            if row["dl"] == 1:
                stat["dup"] += 1
                if not args.dry_run:
                    _drop(path, os.path.join(args.uploads, "_duplicate"), base)
                log(f"  [{i}] 已下载过：{(row['title'] or '')[:24]} → _duplicate/")
                continue

            cat = target_cat(row["category_name"])
            title, author = sanitize(row["title"]), sanitize(row["author"], "佚名")
            name = f"{title}-{author}.epub"
            dst_dir = os.path.join(args.books, cat)
            dst = os.path.join(dst_dir, name)
            if os.path.exists(dst):
                name = f"{title}-{author}_{bid}.epub"
                dst = os.path.join(dst_dir, name)

            stat["moved"] += 1
            if args.dry_run:
                log(f"  [{i}] 预演 → books/{cat}/{name[:36]}")
                continue

            try:
                os.makedirs(dst_dir, exist_ok=True)
                shutil.move(path, dst)
            except Exception as e:
                log(f"  [{i}] 移动失败：{str(e)[:50]}")
                stat["moved"] -= 1
                continue

            rel = os.path.relpath(dst, args.books).replace("\\", "/")
            # epub 里的书名是正确值，顺手修掉列表接口的形近字污染
            new_title = meta["title"] if (meta["title"] and same_enough(meta["title"], row["title"])) \
                else row["title"]
            new_author = meta["author"] if (meta["author"] and same_enough(meta["author"], row["author"])) \
                else row["author"]
            c.execute("UPDATE books SET downloaded=1, downloaded_at=?, file_rel=?, "
                      "title=?, author=? WHERE book_id=?",
                      (int(time.time()), rel, new_title, new_author, bid))
            c.commit()
            log(f"  [{i}] ✅ {cat}/{name[:36]}")
            st.set(done=i, moved=stat["moved"], dup=stat["dup"],
                   unknown=stat["unknown"], broken=stat["broken"])

        log(f"本轮完成：入库 {stat['moved']}｜重复 {stat['dup']}｜"
            f"未知 {stat['unknown']}｜损坏 {stat['broken']}")
        st.set(running=False, done=len(files), moved=stat["moved"], dup=stat["dup"],
               unknown=stat["unknown"], broken=stat["broken"], finished_at=int(time.time()))

    try:
        if args.watch:
            log(f"常驻模式：每 {args.interval} 秒检查一次 {args.uploads}")
            while True:
                try:
                    run_once()
                except Exception as e:
                    log(f"本轮异常：{str(e)[:80]}")
                time.sleep(args.interval)
        else:
            run_once()
        return 0
    finally:
        c.close()


if __name__ == "__main__":
    sys.exit(main())
