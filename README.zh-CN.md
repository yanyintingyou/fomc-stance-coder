# fomc-stance-coder

[English](README.md) | 简体中文

从美联储官网抓取 FOMC 声明与纪要，抽取分节正文，用 OpenAI 兼容接口的大模型对段落做通胀与就业表态编码，再由代码汇总成会议级变量。

## 流程

1. **抓取**：遍历美联储 2008–2025 年 FOMC 索引页，原样保存 HTML，在 `sources.csv` 记录来源、发布日和下载状态。
2. **抽取**：定位正文容器，把小节标题写成 `## ` 标记，归类为标准小节类型，输出纯文本、`sections.csv` 和 `manifest.csv`。
3. **规则基线**：切分句子与子句，按词表匹配主题、维度和方向，汇总净方向占比。没有定出方向或线索冲突的单元保留标记，供人工复核。
4. **LLM 编码**：按“会议×文件”分批送段落，模型在固定轴上给出判断，每条判断必须逐字引用原文，校验不过就丢弃。答案写入缓存，重跑从上次中断处继续。
5. **汇总**：段落方向汇总为会议×文件×主题×维度的 `net`、高置信 `net`、`thin`、混合计数、明确中性计数，以及 state + change 合成变量。

规则基线不需要模型。两层编码都只处理声明正文和纪要中“与会者观点”这一小节。

## 编码口径

主题：

| 代码 | 含义 |
|---|---|
| `inf` | 通胀 |
| `lab` | 就业 |

维度：

| 代码 | 含义 |
|---|---|
| `state` | 当前水平相对 2% 目标或充分就业的位置 |
| `change` | 近期变化方向 |
| `outlook` | 委员会对未来的预期 |

方向取 `-1`、`0`、`1`。提示词要求模型不评价好坏、不把政策目标当成经济判断、不把通胀预期当成委员会展望。

## 仓库结构

```
fomc-stance-coder/
├── _fetch/fetch_fomc_text.py     # 下载 FOMC 原始页面
├── _extract/extract_text.py      # HTML 转文本、分节、清单
├── _code/code_sentences.py       # 规则层句级编码
├── _code/llm_code.py             # LLM 段落编码
├── _code/llm_aggregate.py        # 会议级变量汇总
├── _code/profiles.example.json   # 复制为 profiles.json
├── requirements.txt
├── README.md
└── README.zh-CN.md
```

数据产物不进 git。脚本会在仓库根目录生成 `FOMC statement/`、`FOMC minutes/`、`extracted/`、`coded/`。

## 环境

Python 3.9 及以上。

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## 快速开始

下载与抽取：

```bash
python _fetch/fetch_fomc_text.py
python _extract/extract_text.py
python _code/code_sentences.py   # 可选，规则基线
```

配置模型：

```bash
cp _code/profiles.example.json _code/profiles.json
export LLM_API_KEY=你的密钥      # Windows PowerShell: $env:LLM_API_KEY="你的密钥"
python _code/llm_code.py --list-profiles
python _code/llm_code.py --ping
```

小样本编码：

```bash
python _code/llm_code.py --pilot 12 --workers 2
```

按日期样本编码。CSV 需要 `conference_date` 列，格式 `YYYY-MM-DD`：

```bash
python _code/llm_code.py --event-dates dates.csv --workers 8
```

全量编码：

```bash
python _code/llm_code.py --all --workers 8
```

汇总：

```bash
python _code/llm_aggregate.py --items coded/llm/<tag>/items.jsonl
```

脚本在 `items.jsonl` 同目录写出 `variables.csv`、`paragraph_scores.csv`、`composites.csv`，目标文件已存在时拒绝覆盖。

## 配置

`_code/profiles.json` 存放命名 profile。每个 profile 设置 `base_url`、`model`、`key`（密钥槽名）、`workers`、`timeout`，可选 `rpm`、`think`、`effort`、`think_off`。

密钥读取顺序：

1. `--api-key`
2. 环境变量 `LLM_API_KEY`
3. `_code/keys.json`，按 profile 里的槽名取值
4. `_code/api_key.txt`

`keys.json` 格式：

```json
{"deepseek": "sk-...", "openrouter": "sk-or-..."}
```

本地服务走同一套接口。`base_url` 指向 `http://127.0.0.1:<port>/v1`，密钥留空即可。

`profiles.json`、`keys.json`、`api_key.txt` 不要提交进仓库，`.gitignore` 已列出。

常用参数：

| 参数 | 作用 |
|---|---|
| `--pilot N` | 等距抽取 N 场会议做样本 |
| `--event-dates file.csv` | 只编码 CSV 里列出的日期 |
| `--all` | 编码 2008–2025 全量 |
| `--only-doc statement\|minutes` | 只编码一种文件 |
| `--limit N` | 限制请求数 |
| `--workers N` | 并发请求数 |
| `--rpm N` | 全局每分钟请求上限 |
| `--reparse` | 用缓存答案重建 `items.jsonl` |
| `--tag name` | 指定运行目录名 |

## 输出

| 路径 | 内容 |
|---|---|
| `FOMC statement/`、`FOMC minutes/` | 原始 HTML 与 `sources.csv` |
| `extracted/` | 分节正文、`sections.csv`、`manifest.csv` |
| `coded/*.csv` | 规则层句表和会议变量 |
| `coded/llm/<tag>/items.jsonl` | 保留下来的段落判断 |
| `coded/llm/<tag>/processed.jsonl` | 已编码段落 |
| `coded/llm/<tag>/failures.csv` | 失败段落 ID |
| `coded/llm/<tag>/variables.csv` | 汇总后的会议级变量 |
| `coded/llm/cache/` | 模型原始回答缓存 |

## LLM 层的校验

- 证据必须是段落中不超过 15 个词的连续原文。用省略号跳过中间内容时，各片段要按原顺序出现。
- 证据或附近窗口里必须出现主题词。
- 资产价格短语不计入通胀。
- 通胀预期不计入委员会展望。
- 汇总在代码里完成，模型输出不会直接成为最终变量。
- 批次失败先关掉思考重试，仍失败就逐段重试，失败记录写入 `failures.csv`。
- 连续 15 分钟没有结果时看门狗退出，重跑会补齐缺口。

## 数据来源与限制

- 数据来自 `federalreserve.gov` 的公开 FOMC 页面，覆盖 2008–2025 年。
- 原始 HTML 不改动。抽取阶段修复部分旧页面的编码错位。
- 2008–2009 年纪要没有小节标题，无法自动定位“与会者观点”，两层编码都会跳过这些文件。
- 2020-03-03 会议没有独立纪要。
- 2011-04-27 纪要的“与会者观点”标题跨行，抽取器已处理该边界。
- 规则层覆盖率有限，没有定出方向或线索冲突的单元需要人工复核。
- 编码结果随模型和提示词版本变化，使用前对照原文抽查。

FOMC 声明与纪要是美联储公开出版物，转载时遵守美联储网站的相关条款。

## 许可

MIT，见 [LICENSE](LICENSE)。

本项目是研究工具，与美联储无关。
