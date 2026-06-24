# anthropic-dump-proxy

一个教学用的**透明代理**:可以直接填进 `ANTHROPIC_BASE_URL`,它把请求原封不动转发到真实
MaaS 服务(默认 `https://api.scitix.ai/model-api`),同时把**两端的裸结构体**都 dump 到磁盘,
供学习对照。代理本身不修改任何内容。

## 安装

```bash
pip install -r requirements.txt
```

## 运行

```bash
python proxy.py
```

默认监听 `0.0.0.0:8080`,转发到 `https://api.scitix.ai/model-api`,dump 写到 `./dumps`。

然后让客户端(例如 Claude Code)指向本代理即可:

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8080
export ANTHROPIC_API_KEY=<你的真实 key>   # 代理原样转发,不读取
```

客户端会请求 `http://127.0.0.1:8080/v1/messages`,代理转发到
`https://api.scitix.ai/model-api/v1/messages`,路径、头、body 全部透传。

## 配置(环境变量,均有默认值)

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PROXY_HOST` | `0.0.0.0` | 监听地址 |
| `PROXY_PORT` | `8080` | 监听端口 |
| `UPSTREAM_BASE_URL` | `https://api.scitix.ai/model-api` | 上游真实服务 |
| `DUMP_DIR` | `./dumps` | dump 输出目录 |

## dump 产物(每个请求一个独立目录)

目录名形如 `dumps/20260609T073000_1a2b3c4d/`,里面可能包含:

| 文件 | 内容 |
|------|------|
| `meta.json` | 方法、客户端路径、上游 URL、状态码、是否流式、耗时 |
| `request.headers.json` | 进来的请求头 |
| `request.body.json` | 进来的请求裸结构体(非 JSON 时存 `.txt`/`.bin`) |
| `response.headers.json` | 上游响应头 |
| `response.body.json` | **非流式**响应的裸结构体 |
| `response.stream.sse` | **流式**响应的原始 SSE 事件流(原样) |
| `response.events.json` | 流式事件按顺序解析成的事件数组 |
| `response.reassembled.json` | 把流式事件**重组**回的最终 Message 对象(便于阅读) |

## 流式说明

Claude Code 默认用 SSE 流式(`stream: true`)调 `/v1/messages`。代理对客户端**保持真正的流式
透传**(打字机效果不受影响),同时在后台 tee 一份副本,流结束后再落盘并重组。

## 它**不做**什么

- 不做鉴权、不读 / 不改 API key(原样转发)
- 不修改请求 / 响应 body
- 不重试、不限流、不缓存
