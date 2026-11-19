# -*- coding: utf-8 -*-
"""
每日增量调度器 —— 常驻运行，每天固定时间跑一次增量 + 解码

容器里用法：
    python fq_daily.py            # 每天 03:00（默认）
    FQ_INCR_AT=04:30 python fq_daily.py
"""
import datetime
import os
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
AT = os.environ.get("FQ_INCR_AT", "03:00")
DAYS = os.environ.get("FQ_INCR_DAYS", "3")
PAGES = os.environ.get("FQ_INCR_PAGES", "3")


def log(m):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {m}", flush=True)


def next_run():
    hh, mm = (int(x) for x in AT.split(":"))
    now = datetime.datetime.now()
    t = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if t <= now:
        t += datetime.timedelta(days=1)
    return t


def main():
    log(f"增量调度器启动，每天 {AT} 执行（最近 {DAYS} 天，每类最多 {PAGES} 页）")
    while True:
        t = next_run()
        wait = (t - datetime.datetime.now()).total_seconds()
        log(f"下次执行：{t:%Y-%m-%d %H:%M}（{(wait/3600):.1f} 小时后）")
        time.sleep(max(60, wait))
        log("开始增量扫描")
        rc = subprocess.call(
            [sys.executable, "-u", "fq_incr.py", "--days", DAYS, "--pages", PAGES],
            cwd=BASE)
        log(f"增量结束 rc={rc}，接着刷新解码")
        subprocess.call([sys.executable, "-u", "fq_decode.py"], cwd=BASE)
        time.sleep(120)


if __name__ == "__main__":
    main()
