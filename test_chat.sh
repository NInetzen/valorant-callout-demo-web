#!/bin/bash
set -e
cd /data/workspace/hero-recommender

echo "=== 1. 静态页是否包含 chatbot ==="
curl -s --max-time 5 http://127.0.0.1:8090/index.html | grep -c "CHATBOT"

echo "=== 2. /chat 缺配置提示 ==="
curl -s --max-time 8 -X POST \
  -H "Content-Type: application/json" \
  -d '{"question":"我这套阵容缺什么？","context":{"mapName":"亚海悬城","pickedAgents":["我: 捷风(Jett, 决斗)"]}}' \
  http://127.0.0.1:8091/chat

echo ""
echo "=== 3. /mcp 仍可用 ==="
curl -s --max-time 15 -X POST \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
  http://127.0.0.1:8091/mcp | head -c 120
