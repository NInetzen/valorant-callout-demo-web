#!/usr/bin/env python3
# OP.GG MCP CORS proxy + VALORANT chatbot proxy
# Routes:
#   GET  /health       -> health check
#   POST /mcp          -> forward JSON-RPC requests to https://mcp-api.op.gg/mcp
#   POST /chat         -> aggregate LLM response and return JSON
#   POST /chat_stream  -> stream LLM response to browser as SSE
import hashlib
import http.server
import json
import os
import secrets
import socketserver
import string
import sys
import time
import urllib.error
import urllib.request

UPSTREAM = "https://mcp-api.op.gg/mcp"
PORT = int(os.environ.get("PORT", "8090"))
APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "llm_config.json")
BIND_HOST = os.environ.get("BIND_HOST", "0.0.0.0")

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Accept, Authorization",
    "Access-Control-Max-Age": "86400",
}

COACH_RULES = """你是 VALORANT 阵容教练，服务于一个基于 OP.GG 数据的英雄推荐器 Demo。
回答要求：
1. 全程使用简体中文，语气像游戏教练，直接、清晰、可执行。
2. 严格基于页面上下文回答，不要编造不存在的胜率、样本量或 OP.GG 数据。
3. 如果页面上下文里没有地图、没有已选英雄或没有推荐结果，提醒用户先选择地图、锁定英雄并点击“分析推荐”。
4. 如果推荐结果出现“部分匹配”“职业搭配匹配”“阵容罕见”“仅供参考”“样本少”等提示，要明确说明推荐置信度下降。
5. 回答控制在 3-6 条要点，每条都给具体理由或动作建议。
6. 不要承诺必胜，只能说“更稳”“更适合”“从数据看更常见”。
"""


def safe_json_loads(raw):
    if not raw:
        return {}
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    return json.loads(raw)


def load_config():
    if not os.path.exists(CONFIG_PATH):
        return None, "配置文件不存在：%s" % CONFIG_PATH
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg, None
    except Exception as e:
        return None, "读取配置文件失败：%s" % str(e)


def mask_error(err):
    text = str(err)
    secret_words = ["api_key", "authorization", "Authorization", "Bearer", "x-knot-api-token", "knot_api_token", "gnpc_secret", "X-GNPC-SIGN"]
    for key in secret_words:
        text = text.replace(key, "***")
    return text


def configured(cfg):
    if not cfg:
        return False
    style = (cfg.get("api_style") or "openai_compatible").strip()
    if style == "knot_agui":
        endpoint = (cfg.get("knot_agui_url") or cfg.get("chat_url") or "").strip()
        token = (cfg.get("knot_api_token") or cfg.get("api_key") or "").strip()
        model = (cfg.get("model") or "").strip()
        return bool(endpoint and token and model)
    if style == "gnpc_agent":
        npc_id = (cfg.get("gnpc_npc_id") or "").strip()
        secret = (cfg.get("gnpc_secret") or cfg.get("api_key") or "").strip()
        base_url = (cfg.get("gnpc_base_url") or cfg.get("base_url") or "").strip()
        return bool(npc_id and secret and base_url)
    return bool(cfg.get("api_key") and (cfg.get("chat_url") or cfg.get("base_url")) and cfg.get("model"))


def context_to_background(context):
    return "%s\n\n当前页面上下文 JSON：\n%s" % (COACH_RULES, json.dumps(context or {}, ensure_ascii=False, indent=2))


class ProxyHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=APP_DIR, **kwargs)

    def log_message(self, fmt, *args):
        sys.stderr.write("[proxy] %s - %s\n" % (self.address_string(), fmt % args))

    def _send_cors(self, code=200, ctype="application/json"):
        self.send_response(code)
        for k, v in CORS_HEADERS.items():
            self.send_header(k, v)
        self.send_header("Content-Type", ctype)

    def _send_json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send_cors(code, "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_sse_headers(self):
        self.send_response(200)
        for k, v in CORS_HEADERS.items():
            self.send_header(k, v)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

    def _sse_write(self, obj):
        data = json.dumps(obj, ensure_ascii=False)
        self.wfile.write(("data: %s\n\n" % data).encode("utf-8"))
        self.wfile.flush()

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length > 0 else b"{}"
        return safe_json_loads(raw), raw

    def do_OPTIONS(self):
        self._send_cors(200, "text/plain")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if self.path.startswith("/health"):
            cfg, err = load_config()
            body = {
                "ok": True,
                "service": "hero-recommender-proxy",
                "routes": ["/mcp", "/chat", "/chat_stream"],
                "api_style": (cfg or {}).get("api_style"),
                "llm_configured": configured(cfg),
                "llm_config_error": err,
            }
            self._send_json(body)
            return
        return super().do_GET()

    def do_POST(self):
        if self.path.startswith("/chat_stream"):
            self.handle_chat_stream()
            return
        if self.path.startswith("/chat"):
            self.handle_chat()
            return
        if self.path.startswith("/mcp"):
            self.handle_mcp()
            return
        self._send_json({"ok": False, "error": "Not Found"}, 404)

    def handle_mcp(self):
        try:
            _, body = self._read_json_body()
            req = urllib.request.Request(
                UPSTREAM,
                data=body,
                method="POST",
                headers={
                    "Content-Type": self.headers.get("Content-Type", "application/json"),
                    "Accept": self.headers.get("Accept", "application/json, text/event-stream"),
                    "User-Agent": "opgg-cors-proxy/1.3",
                },
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = resp.read()
                ctype = resp.headers.get("Content-Type", "application/json")
                self._send_cors(resp.status, ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        except urllib.error.HTTPError as e:
            data = e.read()
            self._send_cors(e.code, e.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self._send_json({"jsonrpc": "2.0", "error": {"code": -32603, "message": "proxy_error: %s" % mask_error(e)}}, 502)

    def read_chat_payload(self):
        payload, _ = self._read_json_body()
        question = str(payload.get("question") or "").strip()
        context = payload.get("context") or {}
        conversation_id = str(payload.get("conversation_id") or "").strip()
        return question, context, conversation_id

    def handle_chat(self):
        try:
            question, context, conversation_id = self.read_chat_payload()
            if not question:
                self._send_json({"ok": False, "answer": "请先输入问题。"})
                return
            cfg, err = load_config()
            if err or not cfg:
                self._send_json({"ok": False, "answer": "聊天后端还没配置 LLM：%s" % err})
                return
            style = (cfg.get("api_style") or "openai_compatible").strip()
            if style == "knot_agui":
                answer, conv_id, message_id = self.collect_knot_agui(cfg, question, context, conversation_id, None)
                self._send_json({"ok": True, "answer": answer, "conversation_id": conv_id, "message_id": message_id})
                return
            if style == "gnpc_agent":
                answer, conv_id, message_id = self.collect_gnpc_agent(cfg, question, context, conversation_id, None)
                self._send_json({"ok": True, "answer": answer, "conversation_id": conv_id, "message_id": message_id})
                return
            self.handle_openai_compatible(cfg, question, context)
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")[:1200]
            self._send_json({"ok": False, "answer": "模型接口返回 HTTP %s：%s" % (e.code, raw)})
        except Exception as e:
            self._send_json({"ok": False, "answer": "聊天服务异常：%s" % mask_error(e)}, 502)

    def handle_chat_stream(self):
        try:
            question, context, conversation_id = self.read_chat_payload()
            self._send_sse_headers()
            if not question:
                self._sse_write({"error": "请先输入问题。", "done": True})
                return
            cfg, err = load_config()
            if err or not cfg:
                self._sse_write({"error": "聊天后端还没配置 LLM：%s" % err, "done": True})
                return
            style = (cfg.get("api_style") or "openai_compatible").strip()
            if style == "knot_agui":
                self.collect_knot_agui(cfg, question, context, conversation_id, self._sse_write)
            elif style == "gnpc_agent":
                self.collect_gnpc_agent(cfg, question, context, conversation_id, self._sse_write)
            else:
                self._sse_write({"error": "当前流式显示只支持 Knot AG-UI 或 gNPC Agent。", "done": True})
                return
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except urllib.error.HTTPError as e:
            try:
                raw = e.read().decode("utf-8", "replace")[:1200]
                self._sse_write({"error": "模型接口返回 HTTP %s：%s" % (e.code, raw), "done": True})
            except Exception:
                pass
        except Exception as e:
            try:
                self._sse_write({"error": "聊天服务异常：%s" % mask_error(e), "done": True})
            except Exception:
                pass

    def make_knot_request(self, cfg, question, context, conversation_id):
        endpoint = (cfg.get("knot_agui_url") or cfg.get("chat_url") or "").strip()
        token = (cfg.get("knot_api_token") or cfg.get("api_key") or "").strip()
        user = (cfg.get("knot_api_user") or "").strip()
        model = (cfg.get("model") or "deepseek-v3.1").strip()
        if not endpoint or not token or not model:
            raise RuntimeError("Knot AG-UI 后端已部署，但 llm_config.json 还缺 knot_agui_url、model 或 knot_api_token。")
        body = {
            "input": {
                "message": question,
                "conversation_id": conversation_id,
                "model": model,
                "stream": bool(cfg.get("stream", True)),
                "enable_web_search": bool(cfg.get("enable_web_search", False)),
                "temperature": float(cfg.get("temperature", 0.5)),
                "chat_extra": {
                    "agent_client_uuid": str(cfg.get("agent_client_uuid") or ""),
                    "attached_images": [],
                    "extra_headers": {},
                    "background_knowledge": context_to_background(context),
                },
            }
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream, application/json",
            "x-knot-api-token": token,
            "User-Agent": "valorant-coach-knot-proxy/1.1",
        }
        if user:
            headers["x-knot-api-user"] = user
        for k, v in (cfg.get("extra_headers") or {}).items():
            if v:
                headers[str(k)] = str(v)
        return urllib.request.Request(endpoint, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), method="POST", headers=headers)

    def collect_knot_agui(self, cfg, question, context, conversation_id, emit):
        req = self.make_knot_request(cfg, question, context, conversation_id)
        answer_parts = []
        conv_id = conversation_id
        message_id = ""
        timeout = int(cfg.get("timeout", 60))
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw_line in resp:
                if not raw_line:
                    continue
                line = raw_line.decode("utf-8", "replace").strip()
                if not line:
                    continue
                if line.startswith("data:"):
                    line = line[5:].strip()
                if line == "[DONE]":
                    break
                try:
                    msg = json.loads(line)
                except Exception:
                    continue
                raw = msg.get("rawEvent") or {}
                conv_id = raw.get("conversation_id") or conv_id
                message_id = raw.get("message_id") or message_id
                typ = str(msg.get("type") or "").upper()
                if typ in ("TEXT_MESSAGE_START", "TEXTMESSAGESTART"):
                    answer_parts = []
                    if emit:
                        emit({"start": True, "conversation_id": conv_id, "message_id": message_id})
                elif typ in ("TEXT_MESSAGE_CONTENT", "TEXTMESSAGECONTENT"):
                    delta = raw.get("content") or msg.get("content") or ""
                    answer_parts.append(delta)
                    if emit and delta:
                        emit({"delta": delta, "conversation_id": conv_id, "message_id": message_id})
                elif typ in ("RUN_ERROR", "RUNERROR"):
                    tip = raw.get("tip_option") or {}
                    err = tip.get("content") or raw.get("content") or "Knot agent error"
                    if emit:
                        emit({"error": err, "conversation_id": conv_id, "message_id": message_id, "done": True})
                        return "", conv_id, message_id
                    raise RuntimeError(err)
        final_text = "".join(answer_parts).strip()
        if not final_text:
            final_text = "Knot 已返回但没有解析到正文，请检查 agent 是否输出了 TEXT_MESSAGE_CONTENT 事件。"
        if emit:
            emit({"done": True, "answer": final_text, "conversation_id": conv_id, "message_id": message_id})
        return final_text, conv_id, message_id

    def make_gnpc_request(self, cfg, question, context, conversation_id):
        base_url = (cfg.get("gnpc_base_url") or cfg.get("base_url") or "https://api.gnpc.qq.com").strip().rstrip("/")
        endpoint = (cfg.get("gnpc_chat_url") or cfg.get("chat_url") or (base_url + "/openapi/npc/agent/chat-messages")).strip()
        npc_id = (cfg.get("gnpc_npc_id") or "").strip()
        secret = (cfg.get("gnpc_secret") or cfg.get("api_key") or "").strip()
        user = (cfg.get("gnpc_user") or cfg.get("user") or "zhenyuanni").strip()
        if not endpoint or not npc_id or not secret or not user:
            raise RuntimeError("gNPC 后端已部署，但 llm_config.json 还缺 gnpc_base_url/gnpc_chat_url、gnpc_npc_id、gnpc_secret 或 gnpc_user。")
        ts = str(int(time.time()))
        alphabet = string.ascii_letters + string.digits
        rnd = "".join(secrets.choice(alphabet) for _ in range(12))
        raw_sign = "%s_%s_%s_%s" % (npc_id, secret, ts, rnd)
        sign = hashlib.md5(raw_sign.encode("utf-8")).hexdigest()
        query = "%s\n\n用户问题：%s" % (context_to_background(context), question)
        body = {
            "user": user,
            "query": query,
        }
        # gNPC 测试环境当前不稳定返回 conversationId，且历史 Knot 会话 ID 传入后可能导致只返回 message_end。
        # 默认不向 gNPC 透传前端 conversation_id；如后续确认 gNPC 多轮会话可用，可在配置中开启 gnpc_enable_conversation。
        if conversation_id and bool(cfg.get("gnpc_enable_conversation", False)):
            body["conversationId"] = conversation_id
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream, application/json",
            "X-GNPC-NPCID": npc_id,
            "X-GNPC-TIMESTAMP": ts,
            "X-GNPC-RANDOM": rnd,
            "X-GNPC-SIGN": sign,
            "User-Agent": "valorant-coach-gnpc-proxy/1.0",
        }
        for k, v in (cfg.get("extra_headers") or {}).items():
            if v:
                headers[str(k)] = str(v)
        return urllib.request.Request(endpoint, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), method="POST", headers=headers)

    def collect_gnpc_agent(self, cfg, question, context, conversation_id, emit):
        req = self.make_gnpc_request(cfg, question, context, conversation_id)
        answer_parts = []
        conv_id = conversation_id
        message_id = ""
        timeout = int(cfg.get("timeout", 60))
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw_line in resp:
                if not raw_line:
                    continue
                line = raw_line.decode("utf-8", "replace").strip()
                if not line:
                    continue
                if line.startswith("data:"):
                    line = line[5:].strip()
                if line == "[DONE]":
                    break
                try:
                    msg = json.loads(line)
                except Exception:
                    continue
                event = str(msg.get("event") or msg.get("type") or "").lower()
                conv_id = msg.get("conversationId") or msg.get("conversation_id") or conv_id
                message_id = msg.get("messageId") or msg.get("message_id") or message_id
                if event == "message":
                    delta = msg.get("content") or msg.get("answer") or ""
                    answer_parts.append(delta)
                    if emit and delta:
                        emit({"delta": delta, "conversation_id": conv_id, "message_id": message_id})
                elif event == "message_end":
                    if emit:
                        emit({"done": True, "conversation_id": conv_id, "message_id": message_id})
                    break
                elif event == "error":
                    err = msg.get("message") or msg.get("content") or json.dumps(msg, ensure_ascii=False)
                    if emit:
                        emit({"error": err, "conversation_id": conv_id, "message_id": message_id, "done": True})
                        return "", conv_id, message_id
                    raise RuntimeError(err)
        final_text = "".join(answer_parts).strip()
        if not final_text:
            final_text = "gNPC 已返回但没有解析到正文，请检查是否输出 message 事件。"
        if emit:
            emit({"done": True, "answer": final_text, "conversation_id": conv_id, "message_id": message_id})
        return final_text, conv_id, message_id

    def handle_openai_compatible(self, cfg, question, context):
        api_key = (cfg.get("api_key") or "").strip()
        model = (cfg.get("model") or "").strip()
        chat_url = (cfg.get("chat_url") or "").strip()
        base_url = (cfg.get("base_url") or "").strip().rstrip("/")
        if not chat_url and base_url:
            chat_url = base_url + "/chat/completions"
        if not api_key or not model or not chat_url:
            self._send_json({"ok": False, "answer": "聊天后端已部署，但 llm_config.json 还缺 base_url/chat_url、model 或 api_key。"})
            return
        user_content = "当前页面上下文 JSON：\n%s\n\n用户问题：%s" % (json.dumps(context, ensure_ascii=False, indent=2), question)
        llm_payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": COACH_RULES},
                {"role": "user", "content": user_content},
            ],
            "temperature": float(cfg.get("temperature", 0.35)),
            "stream": False,
        }
        if cfg.get("max_tokens"):
            llm_payload["max_tokens"] = int(cfg.get("max_tokens"))
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": "Bearer " + api_key,
            "User-Agent": "valorant-coach-chatbot/1.0",
        }
        for k, v in (cfg.get("extra_headers") or {}).items():
            if v:
                headers[str(k)] = str(v)
        req = urllib.request.Request(chat_url, data=json.dumps(llm_payload, ensure_ascii=False).encode("utf-8"), method="POST", headers=headers)
        timeout = int(cfg.get("timeout", 30))
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        data = safe_json_loads(raw)
        answer = ""
        choices = data.get("choices") or []
        if choices:
            msg = choices[0].get("message") or {}
            answer = msg.get("content") or choices[0].get("text") or ""
        if not answer:
            answer = data.get("output_text") or data.get("answer") or json.dumps(data, ensure_ascii=False)[:1200]
        self._send_json({"ok": True, "answer": answer})


class ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    print("Starting OP.GG CORS proxy + chatbot proxy on %s:%d" % (BIND_HOST, PORT))
    with ThreadedServer((BIND_HOST, PORT), ProxyHandler) as srv:
        srv.serve_forever()
