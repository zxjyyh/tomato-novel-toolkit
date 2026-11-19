# -*- coding: utf-8 -*-
"""书籍归档器：把散落的 epub 按分类整理进书库目录，并回写数据库

用法：
  python fq_archive.py --dry-run              # 只预演，不动任何文件
  python fq_archive.py --limit 20             # 只处理 20 本（实测用）
  python fq_archive.py                        # 全量执行
  python fq_archive.py --watch                # 常驻监听新文件

规则：
  1. 分类目录 = 数据库 category_name；不足 --min-cat 本的小类并入「其他」
  2. 文件名 = 书名-作者.epub；撞车时加 _书号
  3. 移动后回写 file_rel（相对书库根目录）和 downloaded=1
  4. 读不出书号的 epub 移到 _unidentified/，不阻断流程

注意：本脚本和 fq_ingest.py 是**两套替代方案**，不要同时开。
  fq_ingest 走「收件箱 + 核验 + 分流」流程，fq_archive 是「就地扫全盘归档」，
  同时跑会互相抢同一批文件。
"""
import argparse
import os
import shutil
import sqlite3
import time

from fq_common import ensure_db, read_epub_meta, sanitize


def collect_sources(src, books_root, uploads_root):
    """收集所有待归档 epub（跳过目标书库目录、收件箱、下划线目录）"""
    skip = {os.path.abspath(books_root).lower(), os.path.abspath(uploads_root).lower()
            if uploads_root else ""}
    out = []
    for dp, dns, fns in os.walk(src):
        dns[:] = [d for d in dns
                  if not d.startswith("_")
                  and os.path.abspath(os.path.join(dp, d)).lower() not in skip]
        for fn in fns:
            if fn.lower().endswith(".epub"):
                out.append(os.path.join(dp, fn))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=os.environ.get("FQ_SRC") or ".",
                    help="待归档文件所在根目录")
    ap.add_argument("--books", default=os.environ.get("FQ_BOOKS") or "./books",
                    help="书库根目录")
    ap.add_argument("--uploads", default=os.environ.get("FQ_UPLOADS") or "./uploads",
                    help="收件箱目录（会被跳过，避免和 ingest 抢文件）")
    ap.add_argument("--dry-run", action="store_true", help="只预演，不动文件")
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少本")
    ap.add_argument("--min-cat", type=int, default=500, help="低于此数的分类并入「其他」")
    ap.add_argument("--watch", action="store_true", help="常驻监听新文件")
    ap.add_argument("--interval", type=int, default=20, help="监听轮询间隔秒")
    args = ap.parse_args()

    c = ensure_db()
    c.row_factory = sqlite3.Row

    counts = {r[0]: r[1] for r in c.execute(
        "SELECT COALESCE(NULLIF(category_name,''),'(空)') cat, COUNT(*) "
        "FROM books GROUP BY 1")}

    def target_cat(name):
        nm = (name or "").strip() or "(空)"
        return "其他" if counts.get(nm, 0) < args.min_cat else nm

    # 已归档的 book_id（file_rel 非空即视为已归档）
    done = {r[0] for r in c.execute(
        "SELECT book_id FROM books WHERE COALESCE(file_rel,'')<>''")}
    print(f"数据库已归档：{len(done):,} 本", flush=True)

    def run_once():
        files = collect_sources(args.src, args.books, args.uploads)
        print(f"源目录 epub：{len(files):,} 个", flush=True)

        stat = {"moved": 0, "skip": 0, "noid": 0, "notfound": 0, "collide": 0}
        plan_dirs, batch, processed = {}, [], 0

        for path in files:
            if args.limit and processed >= args.limit:
                break
            meta = read_epub_meta(path)
            bid = meta["book_id"] if meta else None
            if not bid:
                stat["noid"] += 1
                if not args.dry_run:
                    dst_dir = os.path.join(args.books, "_unidentified")
                    os.makedirs(dst_dir, exist_ok=True)
                    try:
                        shutil.move(path, os.path.join(dst_dir, os.path.basename(path)))
                    except Exception:
                        pass
                continue

            row = c.execute("SELECT title, author, category_name FROM books WHERE book_id=?",
                            (bid,)).fetchone()
            if not row:
                stat["notfound"] += 1
                continue
            if bid in done:
                stat["skip"] += 1
                continue

            cat = target_cat(row["category_name"])
            title = sanitize(row["title"])
            author = sanitize(row["author"], "佚名")
            fname = f"{title}-{author}.epub"
            dst_dir = os.path.join(args.books, cat)
            dst = os.path.join(dst_dir, fname)
            if os.path.exists(dst):
                fname = f"{title}-{author}_{bid}.epub"
                dst = os.path.join(dst_dir, fname)
                stat["collide"] += 1

            plan_dirs[cat] = plan_dirs.get(cat, 0) + 1
            processed += 1

            if not args.dry_run:
                os.makedirs(dst_dir, exist_ok=True)
                try:
                    shutil.move(path, dst)
                except Exception as e:
                    print(f"  移动失败 {path}: {e}", flush=True)
                    continue
                rel = os.path.relpath(dst, args.books).replace("\\", "/")
                batch.append((rel, int(time.time()), bid))
                done.add(bid)
                stat["moved"] += 1
                if len(batch) >= 500:
                    c.executemany("UPDATE books SET file_rel=?, downloaded=1, downloaded_at=? "
                                  "WHERE book_id=?", batch)
                    c.commit()
                    batch.clear()

        if not args.dry_run and batch:
            c.executemany("UPDATE books SET file_rel=?, downloaded=1, downloaded_at=? "
                          "WHERE book_id=?", batch)
            c.commit()

        tag = "【预演】" if args.dry_run else ""
        print(f"\n{tag}处理 {processed:,} 本")
        print(f"  移动成功 {stat['moved']:,} | 已归档跳过 {stat['skip']:,} | "
              f"撞名加书号 {stat['collide']:,}")
        print(f"  读不出书号 {stat['noid']:,} | 库里查无此书 {stat['notfound']:,}")
        print("\n分类分布（前 20）：")
        for k, v in sorted(plan_dirs.items(), key=lambda x: -x[1])[:20]:
            print(f"  {k:<12} {v:>6,}")
        print(f"  共 {len(plan_dirs)} 个目录")
        return stat

    try:
        if args.watch:
            print(f"监听模式：每 {args.interval} 秒检查一次 {args.src}", flush=True)
            while True:
                try:
                    run_once()
                except Exception as e:
                    print(f"本轮异常：{e}", flush=True)
                time.sleep(args.interval)
        else:
            run_once()
        return 0
    finally:
        c.close()


if __name__ == "__main__":
    main()
