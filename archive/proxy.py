#!/usr/bin/env python3
"""
Anthropic 兼容的"裸结构体 dump"透明代理 —— 教学用。

用法:
    pip install -r requirements.txt
    python proxy.py
    # 然后让客户端(如 Claude Code)指向本代理:
    #   export ANTHROPIC_BASE_URL=http://127.0.0.1:8080

它做的事只有三件:
    1. 把客户端发来的请求"原封不动"转发到上游真实 MaaS 服务;
    2. 把上游的响应"原封不动"返回给客户端(流式则保持流式);
    3. 把两端的裸结构体(请求 JSON / 响应 JSON / 原始 SSE / 重组后的最终 JSON)
       dump 到磁盘,每个请求一个独立目录,供学习对照。

环境变量(都有默认值,不用配也能跑):
    PROXY_HOST          监听地址            默认 0.0.0.0
    PROXY_PORT          监听端口            默认 8080
    UPSTREAM_BASE_URL   上游服务地址        默认 https://api.scitix.ai/model-api
    DUMP_DIR            dump 输出目录       默认 ./dumps
"""

import os
import io
import json
import time
import uuid
import datetime
import contextlib

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse

# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
PROXY_HOST = os.environ.get("PROXY_HOST", "0.0.0.0")
PROXY_PORT = int(os.environ.get("PROXY_PORT", "8080"))
# 去掉结尾的 "/",避免拼接出双斜杠
UPSTREAM_BASE_URL = os.environ.get(
    "UPSTREAM_BASE_URL", "https://api.scitix.ai/model-api"
).rstrip("/")
DUMP_DIR = os.environ.get("DUMP_DIR", "./dumps")

# 这些头由 httpx / 传输层自己重算或仅对单跳有意义,转发时要剔除,
# 否则会出现 Content-Length 不一致、压缩解释错误等问题。
HOP_BY_HOP_HEADERS = {
    "host",
    "content-length",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    # 让上游决定是否压缩;我们不声明 Accept-Encoding,拿到的就是明文,便于 dump。
    "accept-encoding",
}

# 全局共享一个 httpx 客户端(连接池复用)。不设总超时上限,
# 因为长 SSE 流可能持续很久;只设连接建立超时。
_client: httpx.AsyncClient | None = None

app = FastAPI(title="anthropic-dump-proxy")


@app.on_event("startup")
async def _startup() -> None:
    global _client
    os.makedirs(DUMP_DIR, exist_ok=True)
    timeout = httpx.Timeout(connect=30.0, read=None, write=None, pool=None)
    _client = httpx.AsyncClient(timeout=timeout, follow_redirects=False)
    print(f"[proxy] 监听 http://{PROXY_HOST}:{PROXY_PORT}")
    print(f"[proxy] 转发到 {UPSTREAM_BASE_URL}")
    print(f"[proxy] dump 目录 {os.path.abspath(DUMP_DIR)}")


@app.on_event("shutdown")
async def _shutdown() -> None:
    if _client is not None:
        await _client.aclose()


# --------------------------------------------------------------------------- #
# dump 辅助
# --------------------------------------------------------------------------- #
def _new_dump_dir() -> str:
    """为一次请求创建独立目录:dumps/<UTC时间戳>_<短id>/"""
    now = datetime.datetime.now(datetime.timezone.utc)
    ts = now.strftime("%Y%m%dT%H%M%S_%f")[:-3]  # 毫秒精度
    short = uuid.uuid4().hex[:8]
    path = os.path.join(DUMP_DIR, f"{ts}_{short}")
    os.makedirs(path, exist_ok=True)
    return path


def _safe_write(path: str, data) -> None:
    """写文件,失败只打印不抛出 —— dump 永远不能拖垮转发。"""
    try:
        if isinstance(data, (dict, list)):
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        elif isinstance(data, bytes):
            with open(path, "wb") as f:
                f.write(data)
        else:  # str
            with open(path, "w", encoding="utf-8") as f:
                f.write(data)
    except Exception as e:  # noqa: BLE001
        print(f"[proxy] 写 dump 失败 {path}: {e}")


def _dump_body(dirpath: str, name: str, raw: bytes) -> None:
    """
    把裸 body 落盘。能解析成 JSON 就存成漂亮的 .json,
    否则原样存成 .txt(.bin),保证"什么都不丢"。
    """
    if not raw:
        return
    try:
        obj = json.loads(raw)
        _safe_write(os.path.join(dirpath, f"{name}.json"), obj)
    except (json.JSONDecodeError, UnicodeDecodeError):
        try:
            _safe_write(os.path.join(dirpath, f"{name}.txt"), raw.decode("utf-8"))
        except UnicodeDecodeError:
            _safe_write(os.path.join(dirpath, f"{name}.bin"), raw)


# --------------------------------------------------------------------------- #
# SSE 重组:把 Anthropic 流式事件折叠回一个最终 Message JSON
# --------------------------------------------------------------------------- #
def _parse_sse(text: str) -> list[dict]:
    """把原始 SSE 文本切成事件列表。每个元素是 data 行解析出的 dict。"""
    events: list[dict] = []
    for block in text.split("\n\n"):
        block = block.strip("\n")
        if not block:
            continue
        data_lines = []
        for line in block.split("\n"):
            if line.startswith("data:"):
                data_lines.append(line[len("data:"):].lstrip())
        if not data_lines:
            continue
        payload = "\n".join(data_lines)
        if payload == "[DONE]":
            continue
        try:
            events.append(json.loads(payload))
        except json.JSONDecodeError:
            # 不是 JSON 的 data(少见)就跳过重组,原始 SSE 里仍然保留着。
            continue
    return events


def _reassemble_anthropic(events: list[dict]) -> dict | None:
    """
    依据 Anthropic Messages 流式协议把事件序列重建成最终 Message 对象:
        message_start          -> 取初始 message 骨架
        content_block_start    -> 新建一个内容块
        content_block_delta    -> 累加 text_delta / input_json_delta / thinking_delta
        content_block_stop     -> 收尾(input_json 字符串 -> 解析成对象)
        message_delta          -> 合并 stop_reason / stop_sequence / usage
    无法识别为该协议时返回 None(此时不会写重组文件,但原始 SSE 仍在)。
    """
    message: dict | None = None
    # 记录每个块累积的 partial json 字符串(tool_use 的 input)
    partial_json: dict[int, str] = {}

    for ev in events:
        etype = ev.get("type")
        if etype == "message_start":
            message = ev.get("message", {})
            message.setdefault("content", [])
        elif message is None:
            # 还没拿到 message_start,无法重组
            continue
        elif etype == "content_block_start":
            idx = ev.get("index", len(message["content"]))
            block = ev.get("content_block", {})
            # 补齐到 idx 位置
            while len(message["content"]) <= idx:
                message["content"].append({})
            message["content"][idx] = block
            if block.get("type") == "tool_use":
                partial_json[idx] = ""
        elif etype == "content_block_delta":
            idx = ev.get("index", 0)
            if idx >= len(message["content"]):
                continue
            delta = ev.get("delta", {})
            dtype = delta.get("type")
            block = message["content"][idx]
            if dtype == "text_delta":
                block["text"] = block.get("text", "") + delta.get("text", "")
            elif dtype == "thinking_delta":
                block["thinking"] = block.get("thinking", "") + delta.get(
                    "thinking", ""
                )
            elif dtype == "signature_delta":
                block["signature"] = block.get("signature", "") + delta.get(
                    "signature", ""
                )
            elif dtype == "input_json_delta":
                partial_json[idx] = partial_json.get(idx, "") + delta.get(
                    "partial_json", ""
                )
        elif etype == "content_block_stop":
            idx = ev.get("index", 0)
            if idx in partial_json:
                raw = partial_json[idx]
                try:
                    message["content"][idx]["input"] = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    message["content"][idx]["input"] = raw  # 保底:存原始字符串
        elif etype == "message_delta":
            delta = ev.get("delta", {})
            for k, v in delta.items():
                message[k] = v
            usage = ev.get("usage")
            if usage:
                merged = dict(message.get("usage") or {})
                merged.update(usage)
                message["usage"] = merged
        # message_stop / ping 等无需处理

    return message


# --------------------------------------------------------------------------- #
# 核心:catch-all 透明转发
# --------------------------------------------------------------------------- #
@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
)
async def proxy(path: str, request: Request) -> Response:
    assert _client is not None
    dirpath = _new_dump_dir()
    started = time.time()

    # ---- 1. 收集进来的请求 ---------------------------------------------- #
    method = request.method
    raw_body = await request.body()
    # 过滤掉单跳头
    fwd_headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in HOP_BY_HOP_HEADERS
    }
    query = request.url.query
    upstream_url = f"{UPSTREAM_BASE_URL}/{path}"
    if query:
        upstream_url = f"{upstream_url}?{query}"

    # dump 请求侧裸结构体
    _safe_write(
        os.path.join(dirpath, "request.headers.json"), dict(request.headers)
    )
    _dump_body(dirpath, "request.body", raw_body)

    # 先写一份基础 meta(转发前),后面再补充结果
    meta = {
        "time_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "method": method,
        "client_path": "/" + path,
        "query": query,
        "upstream_url": upstream_url,
    }
    _safe_write(os.path.join(dirpath, "meta.json"), meta)

    # ---- 2. 转发到上游 -------------------------------------------------- #
    upstream_req = _client.build_request(
        method=method,
        url=upstream_url,
        headers=fwd_headers,
        content=raw_body if raw_body else None,
    )

    try:
        upstream_resp = await _client.send(upstream_req, stream=True)
    except Exception as e:  # noqa: BLE001 —— 上游连不上
        meta.update(
            {
                "error": f"上游请求失败: {e!r}",
                "elapsed_ms": round((time.time() - started) * 1000, 1),
            }
        )
        _safe_write(os.path.join(dirpath, "meta.json"), meta)
        print(f"[proxy] 上游请求失败: {e!r}")
        return Response(
            content=json.dumps(
                {"error": {"type": "proxy_upstream_error", "message": str(e)}}
            ),
            status_code=502,
            media_type="application/json",
        )

    # 回给客户端的响应头:同样剔除单跳头(transfer-encoding/content-length 等
    # 由 Starlette 重新计算)
    resp_headers = {
        k: v
        for k, v in upstream_resp.headers.items()
        if k.lower() not in HOP_BY_HOP_HEADERS
    }
    content_type = upstream_resp.headers.get("content-type", "")
    is_stream = "text/event-stream" in content_type.lower()

    # dump 响应头 + 基础结果信息
    _safe_write(
        os.path.join(dirpath, "response.headers.json"),
        dict(upstream_resp.headers),
    )
    meta.update(
        {
            "status_code": upstream_resp.status_code,
            "response_content_type": content_type,
            "streaming": is_stream,
        }
    )

    # ---- 3a. 流式响应:边透传边 tee,流结束后落盘 ---------------------- #
    if is_stream:

        async def stream_and_tee():
            buf = io.BytesIO()
            try:
                async for chunk in upstream_resp.aiter_raw():
                    buf.write(chunk)
                    yield chunk  # 实时透传给客户端
            finally:
                await upstream_resp.aclose()
                raw = buf.getvalue()
                # 原始 SSE 原样保存
                _safe_write(os.path.join(dirpath, "response.stream.sse"), raw)
                # 尝试重组成最终 Message JSON
                try:
                    text = raw.decode("utf-8", errors="replace")
                    events = _parse_sse(text)
                    _safe_write(
                        os.path.join(dirpath, "response.events.json"), events
                    )
                    reassembled = _reassemble_anthropic(events)
                    if reassembled is not None:
                        _safe_write(
                            os.path.join(dirpath, "response.reassembled.json"),
                            reassembled,
                        )
                except Exception as e:  # noqa: BLE001
                    print(f"[proxy] SSE 重组失败: {e}")
                meta["elapsed_ms"] = round((time.time() - started) * 1000, 1)
                _safe_write(os.path.join(dirpath, "meta.json"), meta)
                print(
                    f"[proxy] {method} /{path} -> {upstream_resp.status_code} "
                    f"(stream) dump: {dirpath}"
                )

        return StreamingResponse(
            stream_and_tee(),
            status_code=upstream_resp.status_code,
            headers=resp_headers,
            media_type=content_type or "text/event-stream",
        )

    # ---- 3b. 非流式响应:读全 -> dump -> 一次性返回 ------------------- #
    try:
        body = await upstream_resp.aread()
    finally:
        await upstream_resp.aclose()

    _dump_body(dirpath, "response.body", body)
    meta["elapsed_ms"] = round((time.time() - started) * 1000, 1)
    _safe_write(os.path.join(dirpath, "meta.json"), meta)
    print(
        f"[proxy] {method} /{path} -> {upstream_resp.status_code} "
        f"dump: {dirpath}"
    )

    return Response(
        content=body,
        status_code=upstream_resp.status_code,
        headers=resp_headers,
        media_type=content_type or None,
    )


if __name__ == "__main__":
    uvicorn.run(app, host=PROXY_HOST, port=PROXY_PORT, log_level="info")
