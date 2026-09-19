#!/usr/bin/env bash
# 把一档源的存量补齐 —— 分批跑，留出空档给定时线。
#
# **为什么不能一口气跑完。** run.py 有跨进程锁（data/.run.lock），而且是
# 「拿不到就退出」——不是排队。补张小珺 149 集要约 22 小时 GPU，一直占着锁
# 的话 launchd 那两班日更会连着几十次直接跳过，站上就停更了。
# 所以按批跑：每批做 BATCH 集，批与批之间歇 GAP 秒，让定时线插得进来。
#
# 断点续跑靠两样现成的东西：已发的集不会再被挑中（state.json 的 done），
# 转写按 5 分钟一片落盘（.cache 里的分片缓存），被杀最多损失一片。
#
#   scripts/backfill-source.sh zhangxiaojun 149
set -u
SRC="${1:?用法：backfill-source.sh <source-id> [总集数] }"
WANT="${2:-40}"
BATCH="${BATCH:-8}"
GAP="${GAP:-120}"
DAYS="${DAYS:-1700}"
cd "$(dirname "$0")/.."
set -a; . ~/.config/podcast/env 2>/dev/null; set +a
export JOBS="${JOBS:-4}"

done_n=0
while [ "$done_n" -lt "$WANT" ]; do
  echo "===== $(date -u +%H:%M:%SZ) 批次开始（累计 $done_n/$WANT）====="
  out=$(python3 pipeline/run.py --only "$SRC" --days "$DAYS" \
          --limit "$BATCH" --per-source "$BATCH" --max-words 0 \
          --spend-subscription --no-build 2>&1 \
        | grep -avE "frames/s|^ *[0-9]+%|it/s")
  echo "$out" | tail -4
  n=$(printf '%s' "$out" | grep -c "published ->" || true)
  # **一集都没出就停。** 可能是锁被占、feed 挂了、或者剩下的都判掉了 ——
  # 这时候继续循环只是空转烧电，而且会一直抢锁。
  if [ "$n" -eq 0 ]; then
    echo "本批 0 篇上站，停在这里（累计 $done_n 篇）"; break
  fi
  done_n=$((done_n + n))
  echo "===== 本批 $n 篇，累计 $done_n/$WANT，歇 ${GAP}s 让定时线插队 ====="
  sleep "$GAP"
done
echo "补齐结束：$SRC 本次共 $done_n 篇"
