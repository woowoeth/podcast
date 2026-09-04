#!/bin/bash
# 本机每日跑批 —— 负责云端做不到的那部分。
#
# 为什么需要本机：GitHub Actions 的机房 IP 会被 YouTube 判成机器人并索要
# cookie，所以只在 YouTube 上有文稿的信源（硅谷101、科技早知道等中文播客）
# 在云端永远抓不到。住宅 IP 没这个问题。
#
# 模型凭证按这个顺序找：
#   1) ~/.config/podcast/env 里的 LLM_API_KEY —— 推荐。走 API 账单，
#      完全不碰 Claude 订阅额度。把云端用的那套照抄过来即可：
#        mkdir -p ~/.config/podcast && cat > ~/.config/podcast/env <<EOF
#        LLM_API_KEY=你的key
#        LLM_BASE_URL=https://api.deepseek.com/v1
#        LLM_MODEL=deepseek-chat
#        EOF
#        chmod 600 ~/.config/podcast/env
#   2) 都没有时回退到本机已登录的 claude CLI —— 不需要 key，但会花订阅额度，
#      所以此时刻意压小：每天 2 篇、sonnet、跳过超长集。
#
# 装成每日任务：
#   cp scripts/com.ourword.podcast.plist ~/Library/LaunchAgents/
#   launchctl load ~/Library/LaunchAgents/com.ourword.podcast.plist
# 卸掉：
#   launchctl unload ~/Library/LaunchAgents/com.ourword.podcast.plist
set -uo pipefail

# BASH_SOURCE 可能为空（被 source 进来、或从 stdin 执行），那样会算出 /
REPO="${PODCAST_REPO:-}"
if [ -z "$REPO" ]; then
  SELF="${BASH_SOURCE[0]:-$0}"
  case "$SELF" in
    */*) REPO="$(cd "$(dirname "$SELF")/.." && pwd)" ;;
    *)   REPO="$(pwd)" ;;
  esac
fi
if [ ! -f "$REPO/pipeline/run.py" ]; then
  echo "找不到仓库：REPO=$REPO 里没有 pipeline/run.py。用 PODCAST_REPO=/path 指定。" >&2
  exit 1
fi
cd "$REPO" || exit 1

LOG_DIR="$REPO/.cache/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/$(date +%Y-%m-%d).log"

# launchd 给的 PATH 很窄：claude、python3、yt-dlp、git 都得自己找回来。
export PATH="/Library/Frameworks/Python.framework/Versions/3.14/bin:/opt/homebrew/bin:/usr/local/bin:$HOME/bin/node/bin:$HOME/.local/bin:$PATH"

# 本地凭证（若存在）。600 权限，不进 git。
ENV_FILE="$HOME/.config/podcast/env"
if [ -f "$ENV_FILE" ]; then
  set -a; . "$ENV_FILE"; set +a
  echo "已加载 $ENV_FILE（走 API 账单，不动订阅额度）"
fi

: "${ONLY:=}"
: "${ONLY_RESIDENTIAL:=}"
: "${LIMIT:=2}"
: "${DAYS:=21}"
# 中文的"字数"按字统计，天然比英文词数高（一集中文播客常 2-3 万字，
# 对应的英文集是 1 万词上下），所以上限放到 3 万，否则中文集全被挡掉。
: "${MAX_WORDS:=30000}"
# 没有 API key 时才回退到订阅额度，并把模型压到 sonnet
if [ -z "${LLM_API_KEY:-}" ]; then
  : "${LLM_MODEL:=sonnet}"
  echo "没有 LLM_API_KEY，回退到本机 claude CLI（会花订阅额度）"
  EXTRA="--spend-subscription"
else
  EXTRA=""
fi
export LLM_MODEL

{
  echo "===== $(date '+%F %T') ====="
  command -v claude >/dev/null || { echo "找不到 claude CLI，PATH=$PATH"; exit 1; }
  command -v python3 >/dev/null || { echo "找不到 python3"; exit 1; }

  # state.json 是两条线唯一会真冲突的数据文件。没有这个驱动，冲突会在工作区
  # 留下标记，之后每次 git pull --rebase 都报 "unmerged files"，重试循环
  # 从此全撞在同一面墙上（真出过一次，那轮产出全废）。
  git config merge.podcast-state.driver 'python3 pipeline/mergestate.py %O %A %B'

  # 每轮开头先确认工作区没卡在未合并状态。今天真卡了一整天：heartbeat-cloud.json
  # 留了冲突标记，于是本机线照跑、心跳照写，但**提交和推送全被挡住**，日志里只有
  # 一行 "unmerged files"。原来的脱身逻辑只在推送重试循环里，而卡住的是前面的
  # 提交步骤——所以这一道必须放在最前面，覆盖后面所有路径。
  if [ -n "$(git ls-files -u)" ]; then
    echo "工作区卡在未合并状态，先脱身：$(git ls-files -u | awk '{print $4}' | sort -u | tr '\n' ' ')"
    git rebase --abort 2>/dev/null || git merge --abort 2>/dev/null || true
    if [ -n "$(git ls-files -u)" ]; then
      # abort 也救不回来（比如冲突是 stash pop 留下的），就以远端为准重来：
      # 本机的数据文件名唯一，已推送的不会丢；心跳每轮都会重写。
      git fetch -q origin main && git reset -q --hard origin/main
    fi
  fi

  # 先同步：state.json 在 git 里，云端刚发过的不能重复发
  git pull --rebase --autostash -q origin main || true

  ONLY_ARG=""
  [ -n "$ONLY" ] && ONLY_ARG="--only $ONLY"
  # 默认只跑云端抓不到的那批，避免和云端重复劳动
  [ -n "$ONLY_RESIDENTIAL" ] && ONLY_ARG="$ONLY_ARG --only-residential"

  python3 pipeline/run.py $ONLY_ARG \
      --limit "$LIMIT" --days "$DAYS" --max-words "$MAX_WORDS" $EXTRA
  rc=$?
  echo "run.py exit=$rc"

  # 心跳：这条线跑过没有、跑成什么样，必须留下痕迹。装好 ≠ 在跑——
  # launchd 那次装上之后 runs = 0，一次没触发，而没有任何东西会告诉你。
  # 体检脚本读的就是这个文件（见 pipeline/healthcheck.py）。
  # 心跳换成正式脚本，不再用 heredoc：第一版嵌在被管道接走的花括号块里，
  # 单独执行正常、真跑批却一声不响地没写出文件。心跳自己静默失效等于白做。
  python3 pipeline/heartbeat.py local "$rc" || echo "心跳没写成（不致命，但要查）"

  # residential 源的体检只能在这里做：它们在机房 IP 上必然 403，云端那边的每周
  # 体检对它们没有意义（而且曾经把四档主力源刷到 2/3 次连续失败，差一次就被
  # 自动移除）。每周日跑一次就够，别每天都去敲人家的 feed。
  if [ "$(date +%u)" = "7" ]; then
    python3 pipeline/resolve_sources.py --check --only-residential 2>&1 | tail -20 || true
  fi

  [ "$rc" -ne 0 ] && exit "$rc"

  if [ -z "$(git status --porcelain data/episodes)" ]; then
    # 没有新内容也要把心跳推上去：否则"跑了但闸门全拦下"和"根本没跑"分不开，
    # 而这两件事需要完全不同的处理。
    echo "本轮没有新内容，只推心跳"
    git add data/heartbeat-local.json data/state.json 2>/dev/null || true
    git diff --cached --quiet && exit 0
    git -c user.name="podcast-bot" -c user.email="podcast-bot@users.noreply.github.com" \
        commit -q -m "heartbeat: local"
    git pull -q --rebase --autostash origin main || true
    git push -q origin main || echo "心跳推送失败（不致命）"
    exit 0
  fi

  # 只加数据。原来这里是 git add -A，会把工作区里未提交的源码改动一起扫进
  # bot 的提交，历史就变成误导性的（"build: regenerate site" 里躺着 run.py 的改动）。
  git add data/episodes data/en data/state.json data/sources.json data/heartbeat-local.json 2>/dev/null || true
  n=$(git diff --cached --name-only | grep -c 'data/episodes/' || true)
  git -c user.name="podcast-bot" -c user.email="podcast-bot@users.noreply.github.com" \
      commit -q -m "digest: $n new (local)"
  # 新集要译成英文才会进 /en/。**每条发布线都要接**——原来只有云端日更接了，
  # 本机线（住宅 IP 那条，中文播客主要靠它）发的内容在英文站上一直缺。
  # 失败不阻塞：没译的不渲染，宁可少几篇也不要中英混排。
  python3 pipeline/transspeakers.py || true
  python3 pipeline/translate.py --limit 12 --workers 4 || true
  python3 pipeline/build.py
  # 这张清单每次加新产物都必须跟着改，漏了就是"本机线永远不提交它"——
  # e（分享短链）和 log（更新日志）就漏过：日志里躺着一堆未跟踪的 e/ 目录，
  # 而云端用 git add -A 所以看不出问题，只有本机线在悄悄少推东西。
  # 体检脚本的"数据／正文页／短链三个数字必须相等"就是为了抓这种漏。
  # tw 和 en 也在清单里：三棵树都是产物，漏一棵就是"本机线永远不推它"。
  SITE_FILES="index.html sources s p e log feed.xml sitemap.xml robots.txt 404.html
              search.json llms.txt llms-full.txt icon.svg .nojekyll
              assets tw en"
  # 分页文件是动态数量（cards-1.json … cards-N.json），不能写死一个。
  SITE_FILES="$SITE_FILES $(ls cards-*.json 2>/dev/null | tr '\n' ' ')"
  git add $SITE_FILES 2>/dev/null || true
  git -c user.name="podcast-bot" -c user.email="podcast-bot@users.noreply.github.com" \
      commit -q -m "build: regenerate site" || true

  # 冲突时不做三方合并：生成产物永远从合并后的数据重建。原来这里用
  # `git pull --rebase` + `commit --amend`，两轮跑批同时重建站点必然在
  # index.html / feed.xml 上冲突，rebase 反复失败——云端就是这么丢掉 11 篇的。
  for i in 1 2 3 4 5; do
    if git push -q origin main; then echo "已推送"; exit 0; fi
    # 留下未合并文件就先脱身，否则之后每次 pull 都报 unmerged
    if [ -n "$(git ls-files -u)" ]; then
      git rebase --abort 2>/dev/null || git merge --abort 2>/dev/null || true
      git checkout -q -- data/state.json 2>/dev/null || true
    fi
    echo "push 重试 $i"
    git fetch -q origin main
    # 冲突时不做三方合并。三步都必需：
    #   --mixed  索引对齐远端。用 --soft 的话索引还是旧基线的整棵树，下一个提交会把
    #            这期间别人推上来的源码全部回退——真出过事（见 POSTMORTEM 七）。
    #   取回删除 远端有而本机磁盘没有的数据文件，此刻在 git 眼里是"被删除"，
    #            直接 git add 会把别人刚发的内容删掉。只取回缺失的，不覆盖我们改过的。
    #   reconcile state.json 是索引不是真相，从磁盘重建，不参与合并。
    git reset -q --mixed origin/main
    git diff --name-only --diff-filter=D -- data | while read -r f; do
      git checkout -q -- "$f" 2>/dev/null || true
    done
    python3 pipeline/run.py --reconcile >/dev/null 2>&1 || true
    python3 pipeline/build.py >/dev/null
    git add data/episodes data/state.json data/sources.json data/heartbeat-local.json $SITE_FILES 2>/dev/null || true
    if git diff --cached --quiet; then echo "已是最新，无需推送"; exit 0; fi
    git -c user.name="podcast-bot" -c user.email="podcast-bot@users.noreply.github.com" \
        commit -q -m "digest + build (local)"
    sleep $((RANDOM % 5 + 3))
  done
  echo "push 失败——本轮产出仍在本地，下次跑批会带上"
  exit 1
} 2>&1 | tee -a "$LOG"
