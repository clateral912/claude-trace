"""
mitmproxy addon —— 只把发往 scitix 的请求/响应裸结构体 dump 到磁盘。教学用。

配合 start-mitm.sh 使用:
    mitmweb --mode upstream:http://127.0.0.1:7897 -p 8888 -s dump_scitix.py

设计要点:
  * 只记录 host 含 "scitix" 的流量,其它流量原样透传、不碰。
  * 每个请求一个独立目录 dumps/<UTC时间戳>_<flowid前8>/。
  * 流式(text/event-stream)响应用 tee:边实时透传给客户端、边累积副本,
    流结束再落盘 —— 不破坏 Claude Code 的打字机效果。
  * 非流式(JSON)响应不开 streaming,这样 mitmweb 网页里能完整看到响应体。

环境变量:
    DUMP_DIR            落盘目录       默认 <本文件所在目录>/dumps
    DUMP_TARGET_HOST    目标 host 子串  默认 scitix
"""

import os
import json
import time
import datetime

from mitmproxy import http

DUMP_DIR = os.environ.get(
    "DUMP_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "dumps")
)
TARGET_HOST = os.environ.get("DUMP_TARGET_HOST", "scitix")


def _is_target(flow: http.HTTPFlow) -> bool:
    return TARGET_HOST in flow.request.pretty_host


def _safe_write(path: str, data) -> None:
    """写文件,失败只打印不抛 —— dump 绝不能拖垮转发。"""
    try:
        if isinstance(data, (dict, list)):
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        elif isinstance(data, bytes):
            with open(path, "wb") as f:
                f.write(data)
        else:
            with open(path, "w", encoding="utf-8") as f:
                f.write(data)
    except Exception as e:  # noqa: BLE001
        print(f"[dump] 写失败 {path}: {e}")


def _dump_body(dirpath: str, name: str, raw: bytes, content_type: str) -> None:
    """裸 body 落盘:JSON 存 .json,SSE 存 .sse,其它存 .txt/.bin,什么都不丢。"""
    if not raw:
        return
    try:
        obj = json.loads(raw)
        _safe_write(os.path.join(dirpath, f"{name}.json"), obj)
        return
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass
    if "event-stream" in content_type:
        _safe_write(os.path.join(dirpath, f"{name}.sse"), raw)
        return
    try:
        _safe_write(os.path.join(dirpath, f"{name}.txt"), raw.decode("utf-8"))
    except UnicodeDecodeError:
        _safe_write(os.path.join(dirpath, f"{name}.bin"), raw)


def _update_meta(meta_path: str, updates: dict) -> None:
    try:
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
    except Exception:  # noqa: BLE001
        meta = {}
    meta.update(updates)
    _safe_write(meta_path, meta)


class ScitixDumper:
    # ---- 请求阶段:建目录、落请求裸结构体 ---- #
    def request(self, flow: http.HTTPFlow) -> None:
        if not _is_target(flow):
            return
        # 看门狗健康探针:不落盘(后续 responseheaders/response 因无 dump_dir 自然跳过)
        if "x-mitm-healthcheck" in flow.request.headers:
            return
        now = datetime.datetime.now(datetime.timezone.utc)
        ts = now.strftime("%Y%m%dT%H%M%S_%f")[:-3]  # 毫秒
        dirpath = os.path.join(DUMP_DIR, f"{ts}_{flow.id[:8]}")
        os.makedirs(dirpath, exist_ok=True)
        flow.metadata["dump_dir"] = dirpath
        flow.metadata["t0"] = time.time()

        req = flow.request
        _safe_write(os.path.join(dirpath, "request.headers.json"), dict(req.headers))
        _dump_body(
            dirpath, "request.body", req.content or b"",
            req.headers.get("content-type", ""),
        )
        _safe_write(
            os.path.join(dirpath, "meta.json"),
            {
                "time_utc": now.isoformat(),
                "method": req.method,
                "host": req.pretty_host,
                "path": req.path,
                "url": req.pretty_url,
            },
        )
        print(f"[dump] {req.method} {req.pretty_url} -> {dirpath}")

    # ---- 响应头阶段:落响应头;按是否流式决定 tee 还是缓冲 ---- #
    def responseheaders(self, flow: http.HTTPFlow) -> None:
        if not _is_target(flow):
            return
        dirpath = flow.metadata.get("dump_dir")
        if not dirpath:
            return
        resp = flow.response
        ct = resp.headers.get("content-type", "")
        _safe_write(
            os.path.join(dirpath, "response.headers.json"), dict(resp.headers)
        )
        meta_path = os.path.join(dirpath, "meta.json")
        is_stream = "event-stream" in ct
        _update_meta(
            meta_path,
            {"status_code": resp.status_code, "response_content_type": ct,
             "streaming": is_stream},
        )

        if not is_stream:
            return  # 非流式:不开 streaming,留给 response 钩子落盘(mitmweb 也能看到 body)

        # 流式:tee —— 边透传边累积,流结束落盘
        t0 = flow.metadata.get("t0")
        chunks: list[bytes] = []

        def tee(data: bytes) -> bytes:
            if data:
                chunks.append(data)
            else:  # data == b"" 表示流结束
                body = b"".join(chunks)
                _dump_body(dirpath, "response.body", body, ct)
                if t0:
                    _update_meta(
                        meta_path, {"elapsed_ms": round((time.time() - t0) * 1000, 1)}
                    )
                print(f"[dump] 流式响应落盘 {dirpath} ({len(body)} bytes)")
            return data

        flow.response.stream = tee

    # ---- 响应完成阶段:只处理非流式 body ---- #
    def response(self, flow: http.HTTPFlow) -> None:
        if not _is_target(flow):
            return
        ct = flow.response.headers.get("content-type", "")
        if "event-stream" in ct:
            return  # 流式已在 tee 里落盘
        dirpath = flow.metadata.get("dump_dir")
        if not dirpath:
            return
        _dump_body(dirpath, "response.body", flow.response.content or b"", ct)
        t0 = flow.metadata.get("t0")
        if t0:
            _update_meta(
                os.path.join(dirpath, "meta.json"),
                {"elapsed_ms": round((time.time() - t0) * 1000, 1)},
            )
        print(
            f"[dump] {flow.request.method} {flow.request.pretty_url} "
            f"-> {flow.response.status_code} 落盘 {dirpath}"
        )


addons = [ScitixDumper()]
