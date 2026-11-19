# -*- coding: utf-8 -*-
"""核对磁盘上的 EPUB 与数据库标记是否一致

用途：
  旧库的「已下载」是下载器写的记录，磁盘上的文件才是真实结果
  （可能被删、被搬走、被改名）。这个脚本把两者对一遍。

做法：
  扫目录里的 *.epub → 读 OPF 的 dc:identifier（就是 book_id）→ 和数据库比对

输出三类差异：
  A. 库里标已下载，但磁盘没有文件   → 虚标
  B. 磁盘有文件，但库里没标已下载   → 漏标（可补标）
  C. 两边都有                        → 正常

用法：
    python fq_check_files.py --dir ./books                      # 全量
    python fq_check_files.py --dir ./books --limit 500           # 抽样
    python fq_check_files.py --dir ./books --fix                 # 顺手补标 + 反标
"""
import argparse
import os
import sys
import time

from fq_common import ensure_db, read_epub_meta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--limit", type=int, default=0, help="只扫前 N 个文件（抽样）")
    ap.add_argument("--fix", action="store_true", help="把差异写回数据库")
    args = ap.parse_args()

    t0 = time.time()
    files, bad = [], 0
    for root, _d, fs in os.walk(args.dir):
        for f in fs:
            if f.lower().endswith(".epub"):
                files.append(os.path.join(root, f))
        if args.limit and len(files) >= args.limit:
            break
    if args.limit:
        files = files[:args.limit]
    print(f"发现 {len(files)} 个 epub，开始读 OPF…", flush=True)

    on_disk = {}
    for i, p in enumerate(files, 1):
        meta = read_epub_meta(p)
        bid = meta["book_id"] if meta else None
        if bid:
            on_disk[bid] = p
        else:
            bad += 1
        if i % 5000 == 0:
            print(f"  {i}/{len(files)}，识别 {len(on_disk)} 个书号，"
                  f"耗时 {time.time()-t0:.0f}s", flush=True)

    print(f"读取完成：识别 {len(on_disk)} 个书号，{bad} 个文件读不出书号")

    c = ensure_db()
    try:
        db_ids = {r[0] for r in c.execute("SELECT book_id FROM books")}
        marked = {r[0] for r in c.execute(
            "SELECT book_id FROM books WHERE COALESCE(downloaded,0)=1")}

        disk_ids = set(on_disk)
        a_false = marked - disk_ids          # 标了但没文件
        b_miss = disk_ids - marked           # 有文件但没标
        b_new = disk_ids - db_ids            # 有文件但库里根本没这本
        both = marked & disk_ids

        print("\n=== 核对结果 ===")
        print(f"两边都有（正常）      : {len(both):,}")
        print(f"A 标已下载但无文件    : {len(a_false):,}")
        print(f"B 有文件但没标已下载  : {len(b_miss):,}  其中库里没这本的 {len(b_new):,}")
        print(f"数据库已标记总数      : {len(marked):,}")
        if args.limit:
            print("（抽样模式，A 的数字会偏大，只看比例）")

        if args.fix:
            n1 = n2 = 0
            for bid in b_miss:
                c.execute("UPDATE books SET downloaded=1, file_rel=COALESCE(file_rel,'') "
                          "WHERE book_id=?", (bid,))
                n1 += c.rowcount
                rel = os.path.relpath(on_disk[bid], args.dir).replace("\\", "/")
                c.execute("UPDATE books SET file_rel=? WHERE book_id=? AND COALESCE(file_rel,'')=''",
                          (rel, bid))
            for bid in a_false:
                c.execute("UPDATE books SET downloaded=0, "
                          "note=COALESCE(NULLIF(note,''),'核对：磁盘无文件') WHERE book_id=?", (bid,))
                n2 += c.rowcount
            c.commit()
            print(f"已修正：补标 {n1:,}，反标 {n2:,}")
        return 0
    finally:
        c.close()
        print(f"耗时 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    sys.exit(main())
