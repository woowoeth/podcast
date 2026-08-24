#!/bin/bash
# 本机每日跑批 —— 给拿不到 API key 的部署方式。
#
# 用本机已登录的 claude CLI 生成，所以不需要任何 API key，但会花 Claude 订阅
# 额度。因此这里刻意压到很小：默认每天 2 篇、模型用 sonnet、跳过超过 2 万词的
# 超长集。粗估每天 5 万 input tokens 上下——之前一次性烧光额度是因为拿 opus
# 喂了 90 万。
#
# 装成每日任务：
#   cp scripts/com.ourword.podcast.plist ~/Library/LaunchAgents/
#   launchctl load ~/Library/LaunchAgents/com.ourword.podcast.plist
# 卸掉：
#   launchctl unload ~/Library/LaunchAgents/com.ourword.podcast.plist
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO" || exit 1

LOG_DIR="$REPO/.cache/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/$(date +%Y-%m-%d).log"

# launchd 给的 PATH 很窄：claude、python3、yt-dlp、git 都得自己找回来。
export PATH="/Library/Frameworks/Python.framework/Versions/3.14/bin:/opt/homebrew/bin:/usr/local/bin:$HOME/bin/node/bin:$HOME/.local/bin:$PATH"

: "${LIMIT:=2}"
: "${DAYS:=21}"
: "${MAX_WORDS:=20000}"
: "${LLM_MODEL:=sonnet}"
export LLM_MODEL

{
  echo "===== $(date '+%F %T') ====="
  command -v claude >/dev/null || { echo "找不到 claude CLI，PATH=$PATH"; exit 1; }
  command -v python3 >/dev/null || { echo "找不到 python3"; exit 1; }

  python3 pipeline/run.py \
      --limit "$LIMIT" --days "$DAYS" --max-words "$MAX_WORDS" \
      --spend-subscription
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
