# 番茄书库工具 / Tomato Novel Toolkit

按「受众 × 分类 × 字数档」切片枚举番茄小说「已完结」作品的元数据，落到本地 SQLite，
并提供一个自托管的 Web 面板来管理采集进度、查询与导出。

> [!WARNING]
> **仅供技术学习与个人研究使用。**
> 本项目**不包含、不提供、不附带任何小说正文**，仓库里只有元数据采集与控制台代码。
> 使用本项目抓取数据可能违反内容平台的服务条款，并涉及著作权问题，请自行评估风险，
> 不要将抓取结果用于传播或商业用途。作者不对使用者的行为承担责任。

---

## 技术看点

这个项目真正想解决的是两个采集工程问题，也是代码里最值得看的部分：

| 问题 | 做法 | 位置 |
|---|---|---|
| **字体混淆**：接口返回的书名/字数/在读是私用区码位（`U+E000–U+F8FF`），肉眼是方块 | 用三种系统中文字体做位图渲染，与混淆字体的字形逐一比对余弦相似度，多字体共识投票后输出 `PUA → 真字` 映射表；形近字（犬/大、往/在）靠多字体分歧标记为「低置信」交人工核对 | `fq_fontmap3.py` |
| **限流自适应**：连续抓几千页后接口会静默返回空 body（HTTP 200，无内容） | 每个出口带小时配额 + 连续 2 次空返回判定限流 + 65 分钟冷却 + 多出口轮换；空页要连续重试 3 次才认定「到底了」，避免把限流误判成采集完成 | `fq_enum2.py` |

配套的工程细节：每翻一页落盘断点（中断不丢进度）、按切片区间分给多台机器并行、
采集完成度自检（接口称 1 万本却只拿到几百本时不标记完成）。

## 它能做什么

| 模块 | 说明 |
|---|---|
| **分类计划** | 统计每个分类的总量，生成 `cats.json`，决定哪些分类需要按字数档拆分 |
| **扫描** | 按「受众 × 分类 × 字数档」切片翻页，写入书号与元数据。支持断点续跑、限流自动冷却、多机分片 |
| **补元数据** | 逐本抓详情页，补大类、标签、开书时间（顺带修正被污染的书名） |
| **下载 / 入库**（可选） | 对接外部下载引擎投递任务，核验产出的 epub 后按分类归档。**本仓库不含下载实现** |

## 架构

```
                    ┌──────────────────────────────┐
   浏览器 ─────────▶│  fq_app.py (FastAPI :18081)  │
                    │  扫描 / 下载 / 入库 / 元数据   │
                    └───────┬──────────────────────┘
                            │ 子进程 + 状态文件
        ┌───────────────────┼────────────────────┐
        ▼                   ▼                    ▼
  fq_enum2.py        fq_download.py        fq_ingest.py
  翻页枚举            串行投递下载           核验 + 移动
        │                   │                    │
        ▼                   ▼                    ▼
   fq_books.db  ◀────── uploads/ ──────────▶ books/{分类}/
   (SQLite)             收件箱                 正式书库
```

`fq_books.db` 是唯一状态源：书号、元数据、下载标记、文件相对路径、切片断点都存在里面。
换机器时只需搬数据库和书库目录。

## 环境要求

- Python 3.10+
- 一个可用的代理（可选，长时间抓取建议用，便于控制出口 IP）
- 下载功能需要另行准备下载引擎（本仓库不含）

## 快速开始

```bash
git clone https://github.com/zxjyyh/tomato-novel-toolkit.git
cd tomato-novel-toolkit

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium
```

### 1. 生成字体映射（首次必做）

书库接口返回的书名、字数、在读带有字体混淆字符，需要先建一张映射表才能还原。
准备一个文件放在项目根目录：

- `obf.woff2`：打开 <https://fanqienovel.com/library>，从浏览器 Network 面板里
  把字体请求的 woff2 另存下来

系统中文字体（Windows 自带；Linux 装 `fonts-noto-cjk`）用于比对参照。

```bash
python fq_fontmap3.py      # 产出 mapping.json
```

脚本会用 362 个混淆字形与常用字做位图相似度比对，并列出「低置信」项。
形近字（犬/大、往/在）可能判错，需要人工核对后修正 `mapping.json`。

> 映射表与具体字体文件绑定。平台更换字体后需要重新生成。

### 2. 生成分类计划

```bash
python fq_cats.py --proxy http://127.0.0.1:7890     # 产出 cats.json
```

它会遍历男频/女频全部分类，各请求一次列表接口拿 `total_count`，决定后续怎么切片。

> 没有 cats.json 也能跑（面板会自动退回全分类枚举），但每个小分类都会多查 6 个字档，
> 白白消耗限流配额。

### 3. 扫描

```bash
# 命令行方式（断点续跑，可反复执行）
python fq_enum2.py --plan cats.json --proxies http://127.0.0.1:7890 --delay 2.2
```

### 4. 启动 Web 控制台

```bash
python fq_app.py          # 默认 http://127.0.0.1:18081
```

首次启动会自动建库建表，空库也能正常打开（显示 0 本）。

局域网访问：

```bash
FQ_PANEL_HOST=0.0.0.0 FQ_PANEL_TOKEN='你的口令' python fq_app.py
```

> [!IMPORTANT]
> 面板的接口能启动采集进程、导出全库数据。**监听 `0.0.0.0` 时必须设置 `FQ_PANEL_TOKEN`**，
> 否则同网段任何人都能操作。默认只监听 `127.0.0.1`。

### 配置项

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `FQ_DB` | `./fq_books.db` | 数据库路径 |
| `FQ_MAP` | `./mapping.json` | 字体映射表 |
| `FQ_PLAN` | `./cats.json` | 分类计划 |
| `FQ_PROXY` | 空（直连） | HTTP 代理，如 `http://127.0.0.1:7890` |
| `FQ_PROXIES` | 同上 | 多个出口，逗号分隔（`fq_run_forever.py` 用） |
| `FQ_PANEL_HOST` | `127.0.0.1` | 面板监听地址 |
| `FQ_PANEL_PORT` | `18081` | 监听端口 |
| `FQ_PANEL_TOKEN` | 空 | 设置后 `/api` 请求需带 `X-FQ-Token` 头或 `?token=` |
| `FQ_ENGINE` | `http://127.0.0.1:18430` | 外部下载引擎地址 |
| `FQ_ENGINE_PWD` | 空 | 下载引擎口令（`x-tomato-password` 头） |

完整模板见 [`.env.example`](.env.example)。

## 脚本清单

| 脚本 | 作用 |
|---|---|
| `fq_common.py` | 公共模块：表结构、字体解码、数字解析、文件名清洗、epub 元数据、切片生成、跨进程锁 |
| `fq_app.py` | Web 控制台（FastAPI + 原生前端） |
| `fq_cats.py` | 统计分类总量，生成 `cats.json` |
| `fq_enum2.py` | 全量枚举，支持切片断点、出口轮换、配额 |
| `fq_incr.py` | 增量扫描，只抓最近 N 天新完结的书 |
| `fq_daily.py` | 每日定时调度器（默认 03:00） |
| `fq_run_forever.py` | 守护进程，跑完自动重启，支持多机切片分工 |
| `fq_fontmap3.py` | 生成字体映射表 |
| `fq_decode.py` | 用 `mapping.json` 还原全库被混淆的字段 |
| `fq_meta.py` | 补全大类 / 标签 / 开书时间 |
| `fq_download.py` | 串行投递下载，三种入口（需外部引擎） |
| `fq_push.py` | 只投递不等待的最小投递器（需外部引擎） |
| `fq_ingest.py` | 核验 `uploads/`，匹配书号，移入 `books/` |
| `fq_archive.py` | 就地扫全盘归档 epub（与 `fq_ingest.py` 二选一） |
| `fq_merge_db.py` | 合并多台机器的数据库（按书号去重） |
| `fq_export_tsv.py` | 导出 12 列 TSV 清单 |
| `fq_split_tsv.py` | 把清单按每 N 本切片 |
| `fq_firstchapter.py` | 单独补开书时间 |
| `fq_backfill.py` | 从 epub 元数据回填书名 / 作者 |
| `fq_check_files.py` | 扫描本地 epub，核对下载标记是否准确 |

## 数据字段

`books` 表主要列：

| 列 | 来源 | 说明 |
|---|---|---|
| `book_id` | 扫描 | 主键，19 位数字 |
| `title` / `author` / `abstract` | 扫描 | 列表接口返回，需经字体解码 |
| `category_id` / `category_name` | 扫描 | 细分分类 |
| `word_count_real` / `read_count_real` | 扫描 | 已解析为整数 |
| `last_chapter_ts` | 扫描 | 最后更新时间，近似完结时间 |
| `first_chapter_ts` | 补元数据 | 第一章发布时间，近似开书时间 |
| `top_category` / `tags` | 补元数据 | 大类、角色/情节标签 |
| `downloaded` / `downloaded_at` | 入库 | 是否已入库（**权威字段**） |
| `file_rel` | 入库 / 扫文件 | 相对书库根目录的路径 |

原始混淆文本另存在 `title_raw` / `abstract_raw` / `word_raw` / `read_raw`，
换映射表时可重新解码，不必重爬。

## Docker 部署

```bash
mkdir -p data books uploads
cp mapping.json cats.json data/       # 首次需要
docker compose build
docker compose up -d
```

打开 `http://<主机IP>:18081`。容器启动后**处于停止状态**，需在页面上点「开始」才会抓取。

注意事项：

- 数据库放在本机磁盘，**不要放 NFS/SMB**，SQLite 的 WAL 在共享盘上会锁坏
- 容器内必须监听 `0.0.0.0`（compose 里已设 `FQ_PANEL_HOST`），否则端口映射进不去
- 暴露到局域网时请设置 `FQ_PANEL_TOKEN`
- 不需要代理就去掉 compose 里的 `FQ_PROXY`

## 已知限制

- **单个筛选窗口有上限**：每个「分类 × 字数档」大约只能翻到 1 万本，超出后服务端返回末页。
  因此枚举按切片拆分，而不是一个分类翻到底。
- **列表接口会做字符污染**：少量书名里的字被替换成形近字（公→云、学→半）。
  正文页和 epub 里是正确值，可用 `fq_backfill.py` 回填。
- **下载引擎需自备**：本仓库只负责投递任务和入库，不包含正文下载实现。
- **限流**：连续抓取数小时后会触发冷却，程序会自动等待并续跑，属正常现象。

## 开发

```bash
pip install -r requirements.txt
pip install pytest ruff

python -m pytest              # 单元测试（不需要 playwright / 网络）
ruff check .                  # 风格检查
```

CI 在 Python 3.10 / 3.12 上跑语法检查与单元测试，见 [`.github/workflows/ci.yml`](.github/workflows/ci.yml)。

## License

[MIT](LICENSE)
