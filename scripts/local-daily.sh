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

  # 先同步：state.json 在 git 里，云端刚发过的不能重复发
  git pull --rebase --autostash -q origin main || true

  ONLY_ARG=""
  [ -n "$ONLY" ] && ONLY_ARG="--only $ONLY"

  python3 pipeline/run.py $ONLY_ARG \
      --limit "$LIMIT" --days "$DAYS" --max-words "$MAX_WORDS" $EXTRA
  rc=$?
  echo "run.py exit=$rc"
  [ "$rc" -ne 0 ] && exit "$rc"

  if [ -z "$(git status --porcelain data)" ]; then
    echo "本轮没有新内容，不提交"
    exit 0
  fi

  git add -A data
  n=$(git diff --cached --name-only | grep -c 'data/episodes/' || true)
  git -c user.name="podcast-bot" -c user.email="podcast-bot@users.noreply.github.com" \
      commit -q -m "digest: $n new (local)"
  python3 pipeline/build.py
  git add -A
  git -c user.name="podcast-bot" -c user.email="podcast-bot@users.noreply.github.com" \
      commit -q -m "build: regenerate site" || true

  for i in 1 2 3 4 5; do
    if git push -q origin main; then echo "已推送"; exit 0; fi
    echo "push 重试 $i"
    git pull --rebase --autostash -q origin main || true
    python3 pipeline/build.py; git add -A
    git -c user.name="podcast-bot" -c user.email="podcast-bot@users.noreply.github.com" \
        commit -q --amend --no-edit || true
    sleep $((RANDOM % 5 + 3))
  done
  echo "push 失败"
  exit 1
} 2>&1 | tee -a "$LOG"
