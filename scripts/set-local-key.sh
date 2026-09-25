#!/bin/bash
# 把 Claude 的 API key 写进本机配置，让本机跑批走 API 账单而不是 Claude 订阅额度。
# （不跑这个也行：没有 key 时本机线用已登录的 claude CLI，花订阅额度。）
# 2026-09-25 起只用 Claude，不再用 DeepSeek。
#
#   bash scripts/set-local-key.sh
#
# 输入时不回显、不进 shell 历史、不经过对话。文件权限 600，且在 git 之外。
set -uo pipefail

ENV_DIR="$HOME/.config/podcast"
ENV_FILE="$ENV_DIR/env"

echo "把 Anthropic 的 API key（sk-ant-api 开头；或 claude setup-token 生成的 token）粘贴进来，然后回车（不会显示出来）："
IFS= read -rs KEY
echo

KEY="$(printf '%s' "$KEY" | tr -d '[:space:]')"
if [ -z "$KEY" ]; then
  echo "没读到内容，什么都没改。" >&2
  exit 1
fi

# 形状对不上就先说一声，免得写进去之后跑批才发现
case "$KEY" in
  sk-ant-*) ;;
  *) echo "这个 key 不是 sk-ant- 开头，不像 Anthropic 的，没有写入。" >&2; exit 1 ;;
esac

mkdir -p "$ENV_DIR"
umask 077
cat > "$ENV_FILE" <<EOF
LLM_PROVIDER=anthropic
LLM_API_KEY=$KEY
LLM_MODEL=claude-opus-5
LLM_MODEL_TRIAGE=claude-sonnet-5
LLM_MODEL_REVIEW=claude-opus-5
EOF
chmod 600 "$ENV_FILE"

echo "已写入 $ENV_FILE（权限 600，长度 ${#KEY}）"
echo
echo "验证一下这个 key 能不能用："
cd "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)" || exit 1
set -a; . "$ENV_FILE"; set +a
export PATH="/Library/Frameworks/Python.framework/Versions/3.14/bin:$PATH"
python3 pipeline/whoami.py
