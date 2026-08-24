# 原声 · 播客深读

> **世界太吵，来原声听播客。**
> 每天从 54 档中英文播客里挑出值得记住的判断。要点和金句都带时间戳，点一下就回到
> 它在原声里被说出的那一秒；金句逐字校验过、数字回原文核对过——查不到出处的，
> 一律不上站。

线上： **https://ourword.ai/podcast/** · [RSS](https://ourword.ai/podcast/feed.xml) · [信源清单](https://ourword.ai/podcast/sources/)

---

## 为什么要重做一个

同类站点（如 onepod.site）的做法是：抓 21 个 YouTube 频道的自动字幕，让模型写一篇散记发出来。三个问题：

| | 只抓 YouTube 自动字幕 | 本项目 |
|---|---|---|
| **信源** | 21 个 YouTube 频道，纯音频节目整块缺失 | 54 档，以 Apple Podcasts 官方 RSS 为主干，Acquired / Odd Lots / Invest Like the Best / 中文播客都在 |
| **文稿** | 一层：YouTube 自动字幕，抓不到就发降级篇 | 五层回退，最好的一层是节目自己发布的官方逐字稿（带说话人） |
| **可信度** | 金句无法核对，时间戳没有 | 金句逐字比对逐字稿，对不上当场删；数字回原文核对；时间戳可点击跳原声 |
| **失败时** | 发一篇写着"本机字幕脚本依赖环境异常"的稿子 | 不发。宁可当天零产出 |

第三行是重点。**这个仓库里没有"尽力而为"这个选项**——`pipeline/lib/gate.py` 验不过的记录不会变成页面。

## 内容质量是怎么保证的

### 一、文稿五层回退（`pipeline/lib/transcript.py`）

按质量从高到低试，第一个通过密度校验的胜出：

1. **`feed`** — 节目在 RSS 里发布的 `<podcast:transcript>`（VTT / SRT / JSON）。Odd Lots、TBPN、Practical AI、Think Fast、Acquired 有。JSON 那种还自带说话人分离。
2. **`notes`** — feed 条目本身就是全文。Substack 系（Latent Space、Interconnects）习惯把整份逐字稿贴进 `content:encoded`，格式是 `Joon [00:31:12]: …`，于是**说话人归属和精确时间戳都是免费的**。
3. **`page`** — show notes 里的 transcript 链接，或节目页。抓下来必须**通过标题特征词校验**确认是这一集，否则丢弃（否则会抓到节目的归档页）。
4. **`youtube`** — yt-dlp 拉自动字幕。会做滚动重复裁剪（YouTube 的字幕是"上一条尾部 + 新内容"，直接拼接会把每句说两遍）。
5. **`asr`** — 下载音频送 Whisper 兼容接口转写，带 segment 时间戳。长音频用 ffmpeg 切片。

每一层都要过**语速校验**：英文 70–300 wpm，中文 110–520 字/分。低于下限说明拿到的是 show notes 冒充逐字稿；高于上限说明抓错了文档。两种情况都当作"没有文稿"。

### 二、编辑质检（`pipeline/lib/gate.py`）

| 检查 | 不通过时 |
|---|---|
| 金句在逐字稿里**逐字**存在（允许 `…` 省略，保留的每段都要命中） | 删掉该条金句 |
| `facts` 里的数字能在逐字稿里找到 | 删掉该条数字 |
| 时间戳落在 `0 ~ 时长+180s` 之间 | 删掉该条 |
| 出现管线元信息（yt-dlp / 本机 / 脚本依赖 / 环境异常 / 转写失败…） | **整篇拒绝** |
| 出现空话套话（值得一听 / 干货满满 / 这期节目讨论了…） | 记入日志 |
| 要点 ≥ 5 条、校验通过的金句 ≥ 2 条、标题是中文 | **整篇拒绝** |

剔除了什么、为什么剔除，都写进 run log（Actions artifact 保留 14 天），页面侧栏也会显示"质检剔除 N 处"。

### 三、跨源去重

同一集经常同时出现在 RSS 和节目的 YouTube 频道。指纹 = 归一化标题 + 时长按 2 分钟分桶，命中就跳过。（对照站点没做这个，同一集出现过两次。）

## 怎么自动更新

有两条路，看你能不能拿到 key。

**A · 本机定时（不需要任何 key，当前采用）**

用本机已登录的 `claude` CLI 生成。不产生 API 账单，但会花 Claude 订阅额度，
所以刻意压小：每天 2 篇、模型 sonnet、跳过超过 2 万词的超长集，约 5 万
input tokens/天。

```bash
cp scripts/com.ourword.podcast.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.ourword.podcast.plist
```

每天 10:30 自己跑完整链路并推送。日志在 `.cache/logs/`。停掉用 `launchctl unload`。
代价：机器睡着或断网时不跑（launchd 会在唤醒后补跑）。

**B · GitHub Actions 云端日更（需要一个 key，当前定时已停用）**

`.github/workflows/daily.yml` 的 cron 已注释掉——没有可用 key 时开着只会天天
红灯。拿到 key 后取消注释即可。**不限 Anthropic**：任何 OpenAI 兼容端点都行，
配三个 secret 就跑：

| 供应商 | `LLM_BASE_URL` | `LLM_MODEL` 示例 |
|---|---|---|
| Anthropic | 留空 | `claude-opus-5` |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| **OpenRouter** | `https://openrouter.ai/api/v1` | `deepseek/deepseek-v3.2` |
| 阿里云百炼 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| 智谱 | `https://open.bigmodel.cn/api/paas/v4` | `glm-4.6` |
| Moonshot | `https://api.moonshot.cn/v1` | `kimi-k2-0905-preview` |
| Groq | `https://api.groq.com/openai/v1` | 见其模型列表 |

配完先跑 `python3 pipeline/whoami.py` 验证凭证形态，再跑 `python3 pipeline/selftest.py`
验证全链路。

## 架构

纯静态站 + GitHub Actions 定时任务。Python 只用标准库（`certifi` / `truststore` / `yt-dlp` 是可选增强）。

```
pipeline/
  run.py              编排：拉取 → 去重 → 打分选题 → 取稿 → 生成 → 质检 → 落盘
  build.py            data/ → 静态站（首页 / 单集页 / 信源页 / feed.xml / sitemap）
  resolve_sources.py  信源清单与健康度；feed 迁移时自动从 Apple 目录重解析
  lib/
    net.py            HTTP：重试、退避、磁盘缓存、三级信任库
    feeds.py          RSS 2.0（含 podcast 命名空间）与 YouTube Atom，同一个解析器
    transcript.py     五层文稿回退 + 语速校验 + 章节抽取
    digest.py         结构化深读生成；超长单集走 map-reduce
    gate.py           编辑质检
    util.py           指纹、时间戳、脱敏
data/
  sources.json        54 档信源 + 抓取健康度
  episodes/*.json     每集一个文件（唯一文件名，并发跑不会冲突）
  state.json          已处理、指纹、失败计数
```

**选题打分**（`run.py:score`）：T1 信源 100 分起、T2 55、T3 25；每过一天扣 4 分；自带官方逐字稿 +30；feed 里已有全文 +22；有章节表 +8；短于 15 分钟 −12。每次跑只发前 N 篇。

**失败重试**：取不到文稿的一集重试 3 次后永久跳过，不会每天重烧预算。

## 本地跑

```bash
python3 -m pip install certifi truststore yt-dlp
python3 pipeline/whoami.py                      # 凭证探测：两种 header 都试，不打印密钥
python3 pipeline/selftest.py                    # 全链路自检：信源、模型、转写、取稿
python3 pipeline/resolve_sources.py --check     # 体检信源，顺手修好迁移的 feed
python3 pipeline/run.py --dry-run --days 4      # 只到取稿，不调模型
python3 pipeline/run.py --limit 3               # 真跑三篇
python3 pipeline/build.py                       # 只重建站点
```

没有配任何 API key 时，`lib/llm.py` 会自动改用本机已登录的 `claude` CLI（`claude -p`）。

**这不是零成本。** 不产生 API 账单，但**会花掉你的 Claude 订阅额度**，而且花得很快：
每集要把整份逐字稿（1–3.6 万词）塞进 prompt，超长节目还要按分片各喂一遍。所以
`--limit` 超过 3 时必须显式加 `--spend-subscription`，否则拒绝执行。批量建档请配
`LLM_API_KEY` 走 API。

## 环境变量 / Secrets

| 变量 | 作用 | 不设会怎样 |
|---|---|---|
| `LLM_API_KEY` | `sk-ant-*` 走 Anthropic Messages API，其他走 OpenAI 兼容 `/chat/completions` | 回退到本机 `claude` CLI，**花订阅额度而不是 API 账单** |
| `LLM_BASE_URL` / `LLM_MODEL` | 覆盖端点与模型。**只有设了非 anthropic 的 BASE_URL 才走 OpenAI 兼容路径**，否则一律 Anthropic | Anthropic 默认 `claude-opus-5` |
| `LLM_MODEL_TRIAGE` / `LLM_MODEL_REVIEW` | 选题闸门和成稿评分分别用哪个模型。评审最好换一家——同一个模型给自己的作业打分会偏袒 | 都回落到 `LLM_MODEL` |
| `REVIEW_MIN` / `TRIAGE_MIN` | 成稿评分与选题闸门的及格线 | 8 / 7 |
| `LLM_AUTH` | `bearer` = 把 key 当 OAuth token 发（`Authorization: Bearer` + oauth beta 头）；`api-key` = 发 `x-api-key` | 按前缀自动判断 |
| `TRANSCRIBE_API_KEY` | 第 5 层音频转写（Whisper 兼容） | 第 5 层关闭，只靠前四层 |
| `TRANSCRIBE_BASE_URL` / `TRANSCRIBE_MODEL` | 默认 Groq `whisper-large-v3-turbo` | — |
| `MAX_NEW` / `LOOKBACK_DAYS` | 每次发布上限 / 回溯天数 | 8 / 10 |
| `JOBS` | 并发处理几集 | 3（claude CLI 后端会自动压到 1——多个 headless 会话并跑会直接退出） |
| `PODCAST_BASE` / `PODCAST_SITE` | 部署路径与域名 | `/podcast` · `https://ourword.ai` |

配完 secrets 跑一次 `python3 pipeline/selftest.py`，它会分别报"阻塞"和"降级"：
阻塞项不修定时任务必失败；降级项能跑，但会损失覆盖率。**没有
`TRANSCRIBE_API_KEY` 时，纯音频节目（全部中文播客、Lenny's、Dwarkesh、
Invest Like the Best）拿不到文稿，按规矩就不会上站**——这不是 bug，是那条
"验不过就不发"的规则在起作用。

## 追赶存量

日更任务只看最近 10 天。要把存档做厚，用 `backfill` workflow（手动触发）从
自带官方逐字稿的信源往回补——Odd Lots 1260 集、TBPN 649 集、Practical AI 369 集、
Think Fast 322 集、Latent Space 220 集、Acquired 216 集、Dwarkesh 136 集，
这些的文稿都是免费且带时间码的。

```bash
gh workflow run backfill.yml --repo woowoeth/podcast \
  -f days=180 -f limit=12 -f per_source=4
# 只补某一档
gh workflow run backfill.yml --repo woowoeth/podcast -f only=oddlots -f days=365 -f limit=20
```

它和日更任务共用一把 concurrency 锁，不会互相踩。每源上限默认 4，避免首页被
单一节目占满。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

38 个用例，只覆盖"决定什么能发"的纯函数，不碰网络和模型。用例都是开发中真实
踩到的坑：口语数字（"twenty fourteen"）被当成未验证而删掉、YouTube 滚动字幕
把每句说两遍、章节表冒充逐字稿、同一集算出两个指纹、185 词的一分钟视频被做成
六条要点的深读。

## 版权

站上内容是原播客的中文深读，版权归各节目所有。每一篇都附原节目链接与时间戳，请去支持原作者。代码 MIT。
