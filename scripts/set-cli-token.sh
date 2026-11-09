#!/bin/bash
# 给本机定时任务一个长效 Claude 登录（一年），不再依赖 Claude app 开着、CLI 自己续登录。
#
#   claude setup-token            # 浏览器里点授权，终端最后打出一串 sk-ant-oat01-…
#   bash scripts/set-cli-token.sh # 把那串粘进来
#
# 为什么：11-05 → 11-09 Claude app 关着的四天，定时任务八班全是 403、一篇没出；
# app 一打开，登录自己续上了。长效 token 不需要续期，local-daily.sh 开头从钥匙串读它。
#
# 输入不回显、不进 shell 历史、不经过对话；先拿它真调一次模型，通了才存进钥匙串。
# 存的时候 token 走 stdin，不出现在命令行参数里（ps 看不到）。
set -uo pipefail
SERVICE="${CLI_TOKEN_SERVICE:-ourword-podcast-claude-token}"
export PATH="$HOME/bin/node/bin:$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

echo "把 claude setup-token 打出来的那串 token 粘贴进来，然后回车（不会显示出来）："
IFS= read -rs TOK
echo
TOK="$(printf '%s' "$TOK" | tr -d '[:space:]')"
case "$TOK" in
  sk-ant-oat*) ;;
  "") echo "没读到内容，什么都没改。" >&2; exit 1 ;;
  *) echo "这不像 claude setup-token 生成的 token（应以 sk-ant-oat 开头），没有写入。" >&2; exit 1 ;;
esac
case "$TOK" in
  *[!A-Za-z0-9_-]*) echo "token 里有意外的字符，没有写入。" >&2; exit 1 ;;
esac

echo "先拿它调一次模型……"
out=$(CLAUDE_CODE_OAUTH_TOKEN="$TOK" perl -e 'alarm 120; exec @ARGV' \
      claude -p --model haiku "reply with the single word ok" 2>&1 | tail -3)
# **整行就是 ok 才算通。** 原来写的是 *ok*：报错「Invalid OAuth token」里的 t-o-k-en
# 就含 ok，自测时一个假 token 被当成通过存了进去。
last=$(printf '%s\n' "$out" | tail -1 | tr -d '[:space:].!' | tr '[:upper:]' '[:lower:]')
if [ "$last" != "ok" ]; then
  echo "这个 token 调不通，没有写入：${out:0:200}" >&2
  exit 1
fi

printf 'add-generic-password -U -a %s -s %s -w %s\n' "$USER" "$SERVICE" "$TOK" | security -i >/dev/null
unset TOK
if security find-generic-password -s "$SERVICE" >/dev/null 2>&1; then
  echo "已存进钥匙串（${SERVICE}）。下一轮定时任务起就用它，Claude app 关着也照跑。"
else
  echo "写钥匙串失败，什么都没改。" >&2
  exit 1
fi
