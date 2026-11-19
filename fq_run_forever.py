# -*- coding: utf-8 -*-
"""枚举守护进程 —— 自动续跑，跑到所有切片完成才退出

为什么需要它：
  fq_enum2.py 单次有 --hours 上限（默认 48 小时），到点会正常退出。
  如果没人重启，采集就停在那儿了。这个守护负责「退出就重开」，
  断点存在 slice_progress 表里，重启不丢进度、不重复。

前提：先跑 fq_cats.py 生成 cats.json（否则不知道要跑哪些切片）。

用法（Windows 双击或命令行）：
    python fq_run_forever.py
"""
import os
import sqlite3
import subprocess
import sys
import time

from fq_common import BASE, DB, PLAN, build_slices, load_plan

PROXIES = os.environ.get("FQ_PROXIES", "http://127.0.0.1:7890")
# FQ_PROXIES=none 表示直连（多机协作时，让这台机器走自己的出口 IP）
if str(PROXIES).strip().lower() in ("none", "direct", "-", "off"):
    PROXIES = ""
QUOTA = os.environ.get("FQ_QUOTA", "1500")
SLICE_START = int(os.environ.get("FQ_SLICE_START") or 1)
SLICE_END = int(os.environ.get("FQ_SLICE_END") or 0)


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def progress(keys):
    """只统计本机负责的切片，多机协作时不会因为别人的切片没做而一直等"""
    try:
        c = sqlite3.connect(DB, timeout=30)
        try:
            if keys:
                marks = ",".join("?" * len(keys))
                done = c.execute(
                    f"SELECT COUNT(*) FROM slice_progress WHERE done=1 AND slice_key IN ({marks})",
                    keys).fetchone()[0]
            else:
                done = c.execute("SELECT COUNT(*) FROM slice_progress WHERE done=1").fetchone()[0]
            books = c.execute("SELECT COUNT(*) FROM books").fetchone()[0]
        finally:
            c.close()
        return done, books
    except Exception:
        return -1, -1


def main():
    rows = load_plan(PLAN)
    if not rows:
        log(f"❌ 找不到分类计划 {PLAN}")
        log("   先跑：python fq_cats.py")
        return 1

    ALL = build_slices(rows)
    end = SLICE_END if SLICE_END > 0 else len(ALL)
    mine = ALL[SLICE_START - 1:end]
    if not mine:
        log(f"❌ 切片区间 {SLICE_START}-{end} 为空（全量 {len(ALL)} 个）")
        return 1
    keys = [f"g{g}-c{cid}-w{w}" for g, cid, _n, w in mine]
    total = len(keys)

    if (SLICE_START, end) != (1, len(ALL)):
        log(f"本机负责切片 {SLICE_START}-{end}，共 {total} 个（全量 {len(ALL)}）")

    rounds = 0
    while True:
        done, books = progress(keys)
        if done >= total:
            log(f"全部完成：{done}/{total} 个切片，{books} 本书")
            break
        rounds += 1
        log(f"第 {rounds} 轮启动，当前 {done}/{total} 切片，{books} 本")

        cmd = [sys.executable, "-u", "fq_enum2.py",
               "--plan", PLAN, "--proxies", PROXIES,
               "--quota", str(QUOTA), "--delay", "2.2", "--hours", "24"]
        # 多机协作：本机只跑指定区间（环境变量 FQ_SLICE_START / FQ_SLICE_END）
        if os.environ.get("FQ_SLICE_END") or SLICE_START > 1:
            cmd += ["--slice-start", str(SLICE_START), "--slice-end", str(end)]

        with open(os.path.join(BASE, "enum_loop.log"), "a", encoding="utf-8") as f:
            f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 第 {rounds} 轮 =====\n")
            f.flush()
            rc = subprocess.call(cmd, cwd=BASE, stdout=f, stderr=subprocess.STDOUT)

        done, books = progress(keys)
        log(f"本轮结束 rc={rc}，进度 {done}/{total}，{books} 本")
        if done >= total:
            log("全部完成")
            break
        if rc != 0:
            log("子进程异常退出（可能另一个枚举进程正在跑），等 5 分钟再试")
            time.sleep(300)
        else:
            log("60 秒后继续…")
            time.sleep(60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
