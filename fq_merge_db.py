# -*- coding: utf-8 -*-
"""
合并两台机器各自爬取的数据库

用法：
  python fq_merge_db.py --main fq_books.db --sub /path/to/b_books.db
  python fq_merge_db.py --main fq_books.db --sub /path/to/b_books.db --dry-run

规则：
  - books：按 book_id 去重，主库已有的保留，副库独有的插入
  - slice_progress：done 取或、next_page 取大、total_got 取大
  - 合并前自动备份主库（.bak-时间戳）
"""
import argparse
import os
import shutil
import sqlite3
import time


def cols_of(conn, table, schema="main"):
    return [r[1] for r in conn.execute(f"PRAGMA {schema}.table_info({table})")]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--main", required=True, help="主库路径")
    ap.add_argument("--sub", required=True, help="待合并的库路径")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写入")
    ap.add_argument("--no-backup", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.main) or not os.path.exists(args.sub):
        print("路径不存在")
        return

    if not args.dry_run and not args.no_backup:
        bak = f"{args.main}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        print(f"备份主库 -> {os.path.basename(bak)} ...", flush=True)
        # 必须先把 WAL 合并回主文件：直接 copy 主文件会漏掉还在 -wal 里的事务，
        # 而把 -wal 复制成 xxx.bak-wal 这种名字 SQLite 是不认的，等于没备份。
        ck = sqlite3.connect(args.main, timeout=120)
        try:
            ck.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            ck.close()
        shutil.copy2(args.main, bak)

    c = sqlite3.connect(args.main, timeout=120)
    c.execute("PRAGMA busy_timeout=60000")
    c.execute("ATTACH DATABASE ? AS sub", (args.sub,))

    main_books = c.execute("SELECT COUNT(*) FROM main.books").fetchone()[0]
    sub_books = c.execute("SELECT COUNT(*) FROM sub.books").fetchone()[0]
    overlap = c.execute(
        "SELECT COUNT(*) FROM sub.books s WHERE EXISTS (SELECT 1 FROM main.books m WHERE m.book_id=s.book_id)"
    ).fetchone()[0]
    print(f"主库 {main_books:,} 本 | 副库 {sub_books:,} 本 | 重叠 {overlap:,} | 可新增 {sub_books-overlap:,}")

    # 列对齐：副库可能缺列（比如早期版本没有 file_rel）
    mcols = cols_of(c, "books", "main")
    scols = set(cols_of(c, "books", "sub"))
    missing = [x for x in mcols if x not in scols]
    if missing:
        print(f"副库缺列（用 NULL 填充）：{missing}")
    sel = ", ".join(x if x in scols else "NULL" for x in mcols)
    collist = ", ".join(mcols)

    if args.dry_run:
        print("\n[预演] 不写入。实际执行会：")
        print(f"  1. 插入 {sub_books - overlap:,} 条新书")
        print(f"  2. 合并 slice_progress")
        c.execute("DETACH DATABASE sub")
        c.close()
        return

    t0 = time.time()
    c.execute(f"INSERT OR IGNORE INTO main.books ({collist}) SELECT {sel} FROM sub.books")
    inserted = c.rowcount
    c.commit()
    print(f"插入新书 {inserted:,} 条，耗时 {time.time()-t0:.1f}s")

    # 合并断点表
    before = c.execute("SELECT COUNT(*) FROM main.slice_progress WHERE done=1").fetchone()[0]
    c.execute("""
        INSERT INTO main.slice_progress (slice_key, next_page, total_got, done, updated_at)
        SELECT slice_key, next_page, total_got, done, updated_at FROM sub.slice_progress
        WHERE slice_key NOT IN (SELECT slice_key FROM main.slice_progress)
    """)
    c.execute("""
        UPDATE main.slice_progress SET
            next_page = MAX(next_page, COALESCE((SELECT s.next_page FROM sub.slice_progress s
                          WHERE s.slice_key = main.slice_progress.slice_key), 0)),
            total_got = MAX(total_got, COALESCE((SELECT s.total_got FROM sub.slice_progress s
                          WHERE s.slice_key = main.slice_progress.slice_key), 0)),
            done      = MAX(done,      COALESCE((SELECT s.done FROM sub.slice_progress s
                          WHERE s.slice_key = main.slice_progress.slice_key), 0))
        WHERE slice_key IN (SELECT slice_key FROM sub.slice_progress)
    """)
    c.commit()
    after = c.execute("SELECT COUNT(*) FROM main.slice_progress WHERE done=1").fetchone()[0]
    print(f"已完成切片：{before} -> {after}")

    total = c.execute("SELECT COUNT(*) FROM main.books").fetchone()[0]
    c.execute("DETACH DATABASE sub")
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.close()
    print(f"\n合并完成：主库现有 {total:,} 本")
    print(f"耗时 {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
