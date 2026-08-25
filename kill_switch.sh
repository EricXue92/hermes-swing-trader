#!/usr/bin/env bash
# 急停开关:不经过任何 LLM,直接禁止一切下单/改单。
# 用法:
#   ./kill_switch.sh on    # 激活急停(创建 KILL_SWITCH 文件)
#   ./kill_switch.sh off   # 解除急停
#   ./kill_switch.sh       # 查看状态
DIR="$(cd "$(dirname "$0")" && pwd)"
FLAG="$DIR/KILL_SWITCH"
case "${1:-status}" in
  on)  touch "$FLAG"; echo "🛑 急停已激活:所有交易操作将被工具层拒绝。" ;;
  off) rm -f "$FLAG"; echo "✅ 急停已解除。" ;;
  *)   [ -f "$FLAG" ] && echo "状态: 🛑 急停中" || echo "状态: ✅ 正常" ;;
esac
