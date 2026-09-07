# TokenCoin Gateway

这里是 TokenCoin 的 FastAPI 数据通路和控制通路实现。

## 核心模块

| 文件 | 作用 |
| --- | --- |
| `main.py` | 生命周期、Bearer 鉴权、API、预留与结算、探针调度、状态和指标 |
| `providers.py` | 卖家供给注册、上游 HTTP/SSE 连接、响应密钥脱敏 |
| `router.py` | 选源、首个有效流事件前换源、熔断联动和延迟指标 |
| `ledger.py` | SQLite 事务账本、可续期预留租约、退款、待结算收入和争议状态 |
| `health.py` | 持久化熔断、半开租约、EWMA 延迟和请求代次 |
| `audit.py` | 手动/定时抽检、滚动质量分和安全的检测记录 |
| `metering.py` | Chat/Responses usage 解析、语义输出观测、SSE 累积和 micro-CNY 计价 |
| `telemetry.py` | OpenTelemetry tracing 和 Prometheus metrics |

## API

- `GET /v1/models`：需要 TokenCoin buyer Bearer key
- `POST /v1/chat/completions`：需要鉴权，支持 JSON 和 SSE
- `POST /v1/responses`：需要鉴权，支持 JSON 和 SSE
- `GET /api/status`：前端读取的公开、脱敏运行状态
- `POST /api/probes/run`：需要独立 operator Bearer key，限频触发会实际调用上游并可能消耗上游余额的 canary
- `GET /api/ledger/summary`：需要鉴权，返回本地买家测试余额
- `GET /healthz`：进程健康检查
- `GET /metrics`：Prometheus 文本指标

完整启动方式、买家配置和架构说明见项目根目录 [`README.md`](../README.md)。
