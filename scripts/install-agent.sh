#!/bin/bash
# 装本机定时任务。
#
# 为什么工作副本不能是你日常那份：如果仓库在 ~/Desktop、~/Documents、~/Downloads
# 这类 macOS TCC 保护目录下，LaunchAgent 没有应用身份，读它会得到
#   shell-init: error retrieving current directory: getcwd: ... Operation not permitted
#   /bin/bash: .../local-daily.sh: Operation not permitted
# 到点静默失败，站上不更新也没人会注意——这个坑真踩过（见 POSTMORTEM 八之五）。
# 所以定时任务用自己的一份 clone，放在 ~/Library 下（不受 TCC 保护），
# 自己 pull、自己推，和云端 runner 一个路子。
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/woowoeth/podcast.git}"
AGENT_DIR="${AGENT_DIR:-$HOME/Library/Application Support/ourword-podcast/podcast}"
PLIST="$HOME/Library/LaunchAgents/com.ourword.podcast.plist"
LABEL="com.ourword.podcast"

if [ ! -d "$AGENT_DIR/.git" ]; then
  echo "clone 到 $AGENT_DIR"
  mkdir -p "$(dirname "$AGENT_DIR")"
  git clone -q "$REPO_URL" "$AGENT_DIR"
else
  echo "已有副本，拉一下"
  git -C "$AGENT_DIR" pull -q --rebase --autostash origin main
fi
mkdir -p "$AGENT_DIR/.cache/logs"

# state.json 的合并驱动：git config 是仓库本地设置，clone 不会带过来，
# 所以每份工作副本都得配一次。
git -C "$AGENT_DIR" config merge.podcast-state.driver 'python3 pipeline/mergestate.py %O %A %B'

echo "写 $PLIST"
mkdir -p "$(dirname "$PLIST")"
sed -e "s#__DIR__#$AGENT_DIR#g" "$AGENT_DIR/scripts/com.ourword.podcast.plist" > "$PLIST"
plutil -lint "$PLIST"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "已装。计划时间（每小时 :30；正点班次见 pipeline/slot.py 的 SLOTS）："
plutil -p "$PLIST" | grep -A3 StartCalendarInterval

cat <<'NOTE'

装上时（RunAtLoad）就会拉一次。先看它判断了什么再信它——从没跑过的定时任务等于没有：
  tail "$HOME/Library/Application Support/ourword-podcast/podcast/.cache/logs/slot.log"
最近一班已经跑完时，整点和 kickstart 都只记一行 skip、不跑。要真跑一轮，直接执行脚本
（已经有一轮在跑就会拒绝）：
  bash "$HOME/Library/Application Support/ourword-podcast/podcast/scripts/local-daily.sh"
stderr 有东西就是环境问题（PATH、权限、密钥），不是内容问题：
  cat "$HOME/Library/Application Support/ourword-podcast/podcast/.cache/logs/launchd.err"
NOTE
