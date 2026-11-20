#!/usr/bin/env bash
# 人物频道连着补：两班日更之间一批接一批跑 pipeline/people.py，直到线索做完或到点。
#
# 2026-11-19 用户：「尽快把人物频道内容完善好」。日更里每轮只顺带 4 篇，700 多条线索要两个月；
# 这里在日更的空档里连着跑。规矩和 backfill-source.sh 一样：
#   · 同一套省 token 的模型（守护 BackfillUsesTheLeanModels 盯着三份脚本一致）；
#   · 每批之前先让出日更（日更在跑就等），people.py 自己拿跑批锁；
#   · 发布靠日更那一班的建站 + 推送——这里只产稿，不碰 git。
# 限流：一批里被 YouTube 限流提前停（people-runs.json 的 stopped）就歇久一点再来，
# 硬撞只会给更多好视频记上失败。
#
#   scripts/backfill-people.sh                  # 默认每批发 6 篇，跑到 21:00
#   UNTIL=09:45 BATCH=4 scripts/backfill-people.sh
set -u
if [ -z "${BACKFILL_COPY:-}" ]; then
  export BACKFILL_REPO="$(cd "$(dirname "$0")/.." && pwd)"
  _copy="$(mktemp "${TMPDIR:-/tmp}/backfill-people.XXXXXX")"
  cp "$0" "$_copy"
  BACKFILL_COPY=1 exec bash "$_copy" "$@"
fi
BATCH="${BATCH:-6}"
GAP="${GAP:-60}"
THROTTLE_REST="${THROTTLE_REST:-1800}"
# 连着限流就加倍歇（30 → 60 → 120 分钟封顶）：「要登录确认不是机器人」通常要几个小时才解开，
# 每 30 分钟去撞一次只会把解封往后推（见 lib/transcript.py 的限流熔断）。
REST_MAX="${REST_MAX:-7200}"
UNTIL="${UNTIL:-21:00}"
cd "${BACKFILL_REPO:?}"
set -a; . ~/.config/podcast/env 2>/dev/null; set +a

if [ -z "${LLM_API_KEY:-}" ]; then
  : "${LLM_MODEL:=sonnet}"
  : "${LLM_MODEL_DIGEST:=sonnet}"
  : "${LLM_MODEL_TRIAGE:=haiku}"
  : "${LLM_MODEL_MAP:=haiku}"
  : "${LLM_MODEL_REVIEW:=haiku}"
  export LLM_MODEL LLM_MODEL_DIGEST LLM_MODEL_TRIAGE LLM_MODEL_MAP LLM_MODEL_REVIEW
fi

past_until() {
  local now end
  now=$((10#$(date +%H%M))); end=$((10#${UNTIL/:/}))
  [ "$now" -ge "$end" ]
}
wait_turn() {
  local said=""
  while pgrep -f '^(/bin/)?bash .*scripts/local-daily\.sh( |$)' >/dev/null; do
    [ -z "$said" ] && echo "----- $(date +%H:%M) 日更在跑，先让出来 -----" && said=1
    sleep 120
  done
}
last() {  # people-runs.json 最后一行的某个字段
  python3 -c "import json,sys;r=json.load(open('data/people-runs.json'))[-1];v=r.get(sys.argv[1]);print('' if v is None else v)" "$1" 2>/dev/null
}

total=0; miss=0; rest=$THROTTLE_REST
while ! past_until; do
  wait_turn
  before=$(last at)
  echo "----- $(date +%H:%M) 批次开始（累计 $total 篇）-----"
  # 一开始就走音频＋本机转写：字幕一两条之后必被限流（2026-11-20，见 people.run_next）
  PEOPLE_AUDIO_FIRST="${PEOPLE_AUDIO_FIRST:-1}" python3 pipeline/people.py --next "$BATCH" 2>&1 | grep -avE "frames/s|^ *[0-9]+%|it/s"
  if [ "$(last at)" = "$before" ]; then
    miss=$((miss + 1))
    # 连着几批都没留痕 = people.py 在报错，不是锁被占（锁被占时 wait_turn 已经等过日更）
    if [ "$miss" -ge 5 ]; then echo "########## 连续 $miss 批没跑成，停下等人看 ##########"; break; fi
    echo "----- 这一批没跑成（锁被占或出错），歇 ${GAP}s -----"; sleep "$GAP"; continue
  fi
  miss=0
  n=$(last published); tried=$(last tried); left=$(last pending)
  total=$((total + ${n:-0}))
  echo "----- 本批试 ${tried} 发 ${n}，累计 $total 篇，剩 ${left} 条线索 -----"
  if [ "${tried:-0}" = "0" ]; then echo "########## 线索做完了 ##########"; break; fi
  if [ -n "$(last stopped)" ]; then
    echo "----- YouTube 限流，歇 $((rest / 60)) 分钟 -----"; sleep "$rest"
    rest=$((rest * 2)); [ "$rest" -gt "$REST_MAX" ] && rest=$REST_MAX
    continue
  fi
  rest=$THROTTLE_REST
  sleep "$GAP"
done
echo "########## $(date +%H:%M) 人物频道补跑结束：本次共 $total 篇 ##########"
