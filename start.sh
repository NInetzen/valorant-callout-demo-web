#!/bin/bash
cd /data/workspace/app

# 杀掉旧的 8090/8091 进程
fuser -k 8090/tcp 2>/dev/null
fuser -k 8091/tcp 2>/dev/null
sleep 1

# 启动静态文件服务 (8090)
nohup /usr/bin/python3 -m http.server 8090 > /data/workspace/app/http8090.log 2>&1 &

# 启动代理服务 (8091)
nohup /usr/bin/python3 /data/workspace/app/opgg_proxy.py > /data/workspace/app/proxy8091.log 2>&1 &

echo "Services started: 8090 (static) + 8091 (proxy)"
