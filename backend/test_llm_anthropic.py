#!/usr/bin/env python3
"""Anthropic 适配层本地验收（mock SSE 服务器，真实走 httpx 流式）。
断言：
A. reasoning_effort=low → 请求带 thinking=disabled，正常拿到正文
B. 网关 400 拒绝 thinking 参数 → 阶梯降级到小预算（且按约束去掉 temperature）→ 拿到正文
C. 思考烧光预算（stop=max_tokens 零正文）→ 报错如实带 stop 与思考字符数
D. OpenAI 兼容通道回归：不受影响
"""
import asyncio
import json
import threading
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from app import llm  # noqa: E402

requests_log = []
scenario = {"mode": "A"}


def sse(events):
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode()


def text_stream(text):
    return [
        {"type": "message_start", "message": {"usage": {"input_tokens": 10}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
    ]


def thinking_only_stream(chars=3000):
    return [
        {"type": "message_start", "message": {"usage": {"input_tokens": 10}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "thinking_delta", "thinking": "想" * chars}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "max_tokens"}},
    ]


class Mock(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        requests_log.append(body)
        mode = scenario["mode"]
        if mode == "A":
            # 校验 low 时确实走了 adaptive + effort=low（claude-opus-5-5 唯一接受的形态）
            assert body.get("thinking") == {"type": "adaptive"}, f"payload={body.get('thinking')}"
            assert body.get("output_config") == {"effort": "low"}, f"output_config={body.get('output_config')}"
            payload = sse(text_stream("print('ok')"))
            self._send(200, payload)
        elif mode == "B":
            if body.get("thinking"):
                self._send(400, json.dumps(
                    {"error": {"message": "requires adaptive thinking; omit thinking or use adaptive"}}).encode(),
                    ctype="application/json")
                return
            payload = sse(text_stream("fixed code"))
            self._send(200, payload)
        elif mode == "C":
            self._send(200, sse(thinking_only_stream()))
        elif mode == "D":
            payload = (f'data: {json.dumps({"choices": [{"delta": {"content": "openai-ok"}}]})}\n\n'
                       'data: [DONE]\n\n').encode()
            self._send(200, payload)
        else:
            self._send(500, b"{}")

    def _send(self, code, payload, ctype="text/event-stream"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):
        pass


server = HTTPServer(("127.0.0.1", 0), Mock)
threading.Thread(target=server.serve_forever, daemon=True).start()
PORT = server.server_address[1]

P_ANTH = {"name": "cc-mock", "type": "anthropic", "base_url": f"http://127.0.0.1:{PORT}",
          "api_key": "k", "model": "claude-opus-5-5"}
P_OAI = {"name": "oai-mock", "type": "openai", "base_url": f"http://127.0.0.1:{PORT}",
         "api_key": "k", "model": "m"}

results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")


async def main():
    msgs = [{"role": "user", "content": "写代码"}]

    scenario["mode"] = "A"
    requests_log.clear()
    text = await llm.complete(P_ANTH, msgs, max_tokens=32768, reasoning_effort="low")
    check("A1 low 推理走 adaptive+effort=low 且拿到正文", text == "print('ok')")
    check("A2 请求体含 adaptive thinking", requests_log[0].get("thinking") == {"type": "adaptive"})

    scenario["mode"] = "B"
    requests_log.clear()
    text = await llm.complete(P_ANTH, msgs, max_tokens=32768, reasoning_effort="low")
    check("B1 400 拒绝后降级省略 thinking 拿到正文", text == "fixed code")
    check("B2 共发 2 次请求（adaptive→省略）", len(requests_log) == 2,
          f"n={len(requests_log)}")
    check("B3 第二次请求不带 thinking/output_config",
          "thinking" not in requests_log[1] and "output_config" not in requests_log[1])

    scenario["mode"] = "C"
    requests_log.clear()
    err = None
    try:
        await llm.complete(P_ANTH, msgs, max_tokens=32768)
    except llm.ProviderError as e:
        err = str(e)
    check("C1 思考烧光预算如实报 empty", err is not None and "[empty]" in err, (err or "")[:60])
    check("C2 报错带 stop=max_tokens 与思考字符数",
          err is not None and "stop=max_tokens" in err and "思考消耗约" in err,
          (err or "")[:120])
    check("C3 自动以 low 重试过（≥2 次请求）", len(requests_log) >= 2, f"n={len(requests_log)}")

    scenario["mode"] = "D"
    text = await llm.complete(P_OAI, msgs, max_tokens=8192)
    check("D1 OpenAI 兼容通道回归正常", text == "openai-ok")

    server.shutdown()
    print(f"\n===== {sum(results)}/{len(results)} 通过 =====")
    if not all(results):
        sys.exit(1)

asyncio.run(main())
