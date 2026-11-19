# 部署说明

两种方式：本机直接跑（开发 / 单机使用），或 Docker（NAS / 常开小主机）。

---

## 一、目录结构

建议所有数据集中在一个目录下，方便整体迁移：

```
tomato/
├── data/                数据库与配置（必须在本机磁盘）
│   ├── fq_books.db
│   ├── mapping.json
│   └── cats.json
├── books/               正式书库
│   └── {分类}/{书名}-{作者}.epub
└── uploads/             收件箱
    ├── _duplicate/      已下载过的重复文件
    ├── _unknown/        库里没有这本书
    └── _broken/         文件损坏
```

> **数据库不要放 SMB / NFS 共享目录。** SQLite 的 WAL 模式在共享盘上会锁冲突，
> 轻则报 `database is locked`，重则损坏数据库。

---

## 二、Docker 部署

### 1. 准备

```bash
git clone <repo-url> && cd tomato-novel-toolkit
mkdir -p data books uploads

# 首次生成的映射表与分类计划放进 data/
#   mapping.json 来自 fq_fontmap3.py
#   cats.json    来自 fq_cats.py
cp mapping.json cats.json data/
```

### 2. 构建

```bash
docker compose build
```

首次构建约 10~20 分钟（要下载 Chromium）。国内网络默认走清华 pip 源和阿里云 apt 镜像，
如需更换：

```bash
docker compose build --build-arg PIP_INDEX_URL=https://pypi.org/simple --build-arg APT_MIRROR=deb.debian.org
```

### 3. 启动

```bash
docker compose up -d
docker logs -f tomato-panel --tail 50
```

打开 `http://<主机IP>:18081`。

**容器启动后处于停止状态**，必须在页面上点「开始全量枚举」或调接口才会抓取：

```bash
# mode 取值：full（全量，读 cats.json）/ single（单个分类）/ incr（增量）
curl -X POST http://<主机IP>:18081/api/scan/start \
     -H "Content-Type: application/json" \
     -H "X-FQ-Token: <你的口令，没设就删掉这行>" \
     -d '{"mode":"full"}'
```

---

## 三、本机直接跑

```bash
pip install -r requirements.txt
python -m playwright install chromium

export FQ_DB=./data/fq_books.db
export FQ_MAP=./data/mapping.json
export FQ_PLAN=./data/cats.json
export FQ_PROXY=http://127.0.0.1:7890     # 不需要代理就不设

# 默认只监听 127.0.0.1。要局域网访问就改成 0.0.0.0，并务必设口令
export FQ_PANEL_HOST=127.0.0.1
# export FQ_PANEL_TOKEN='你的口令'

python fq_app.py
```

Windows 用 `set` 代替 `export`，或用 PowerShell 的 `$env:FQ_DB = "..."`。

---

## 四、日常运维

### 查看进度

```bash
curl -s http://127.0.0.1:18081/api/stats

# 或直接查库
docker exec tomato-panel python -c "
import sqlite3; c=sqlite3.connect('/data/fq_books.db')
print('书籍', c.execute('select count(*) from books').fetchone()[0])
print('切片', c.execute('select count(*) from slice_progress where done=1').fetchone()[0])
"
```

### 备份

```bash
# 先合并 WAL，再拷贝，避免拷到半截状态
docker exec tomato-panel python -c "
import sqlite3; c=sqlite3.connect('/data/fq_books.db')
c.execute('PRAGMA wal_checkpoint(TRUNCATE)'); c.close()"

cp data/fq_books.db data/backup_$(date +%F).db
```

书库目录直接 `rsync` / `robocopy` 即可。

### 日志里的正常现象

| 日志 | 含义 |
|---|---|
| `所有出口都不可用，等 XX 分钟` | 触发访问频率限制，程序在冷却。会自动恢复，不用干预 |
| `⚠ 代理连续失败，冷却 65 分钟` | 代理出口被限，冷却后重试 |
| `切片完成：N 页，M 本` | 正常，一个切片跑完了 |

判断标准：**连续 2 小时没有新日志**才是真卡住了。先确认代理是否可用，
可用就重启进程（断点在库里，不会丢进度）。

### 多机并行

枚举可以拆到多台机器，各自跑不同的切片区间，最后合并数据库。

切片总数取决于你的分类数量，启动时日志第一行会打印「计划：N 个切片」。按 N 对半切：

```bash
# 机器 A：前一半
FQ_SLICE_START=1   FQ_SLICE_END=300 python fq_run_forever.py

# 机器 B：后一半（只给起点，表示一直跑到最后一个）
FQ_SLICE_START=301 python fq_run_forever.py
```

```bash
# 合并（会先备份主库）
python fq_merge_db.py --main fq_books.db --sub b_books.db --dry-run
python fq_merge_db.py --main fq_books.db --sub b_books.db
```

注意：**每台机器必须有独立的出口 IP**，否则共用同一个配额，达不到提速效果。

---

## 五、收件箱工作流

多台机器下载的书统一丢进 `uploads/`，由入库模块统一核验：

```
uploads/书名-作者.epub
   ├─ 读不出书号 / zip 损坏   → _broken/
   ├─ 库内查无此书            → _unknown/
   ├─ 库内已标记已下载        → _duplicate/
   └─ 匹配成功                → books/{分类}/，标记 downloaded=1
```

扫描器会检查文件大小是否在若干秒内不再变化，避免处理到正在写入的半截文件。

可以手动点「扫描入库」，也可以常驻：

```bash
python fq_ingest.py --watch --interval 300
```

---

## 六、常见问题

**Q: 书名里出现生僻字 / 乱码**
字体映射表与平台当前字体绑定。平台换字体后映射失效，需要重新抓 `obf.woff2` 并运行
`fq_fontmap3.py`，再跑 `fq_decode.py` 重刷全库（原始文本存在 `*_raw` 列里，不用重爬）。

**Q: 书名少了一两个字**
列表接口对字符做了形近字替换。epub 里是正确值，用 `fq_backfill.py` 回填。

**Q: 扫描很快结束，但数量明显偏少**
某个切片可能在中途返回空页。程序会重试三次；仍失败则不标记该切片完成，下次继续。

**Q: 面板显示「扫描中」，但日志早就不动了**
面板用跨进程锁 `fq_books.db.scan.lock` 判断扫描是否在跑：谁在跑谁占锁，进程退出后锁由系统
自动释放。如果显示运行中但确实没进程，说明锁被别的程序占着（比如同时开了两个容器挂同一份
数据库）。先 `ps` 确认，再删掉锁文件重试。

**Q: 面板点了「开始」没反应，日志里是 FileNotFoundError: cats.json**
先跑 `python fq_cats.py` 生成分类计划。直接命令行跑 `fq_enum2.py --all` 可以跳过这一步，
但面板默认走 `--plan`（没有文件时会自动退回 `--all`，只是切片更多、更费配额）。

**Q: 页面查询很慢**
81 万行时按 `COALESCE(field,0)` 排序会全表扫。建部分索引：

```sql
CREATE INDEX IF NOT EXISTS idx_dl1  ON books(book_id) WHERE downloaded=1;
CREATE INDEX IF NOT EXISTS idx_meta1 ON books(book_id) WHERE meta_fetched_at>0;
CREATE INDEX IF NOT EXISTS idx_meta_read ON books(read_count_real) WHERE COALESCE(meta_fetched_at,0)=0;
```
