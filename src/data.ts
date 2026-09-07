import type { ModelMarket, SupplyHealthStatus } from "./types";

export const models: ModelMarket[] = [
  {
    id: "deepseek-v4-flash",
    provider: "deepseek",
    name: "DeepSeek V4 Flash",
    shortName: "DS",
    capability: "代码与推理",
    color: "#3857d6",
  },
  {
    id: "kimi-k2-6",
    provider: "kimi",
    name: "Kimi K2.6",
    shortName: "KM",
    capability: "长文本与搜索",
    color: "#7956d8",
  },
  {
    id: "minimax-m2-7",
    provider: "minimax",
    name: "MiniMax M2.7",
    shortName: "MM",
    capability: "Agent 与工具",
    color: "#d66a34",
  },
  {
    id: "glm-5-turbo",
    provider: "glm",
    name: "GLM-5 Turbo",
    shortName: "GL",
    capability: "通用与编程",
    color: "#24836f",
  },
];

export const healthLabels: Record<SupplyHealthStatus, string> = {
  healthy: "正常",
  degraded: "性能下降",
  open: "已隔离",
  half_open: "恢复观察",
};

export const checkTypeLabels: Record<string, string> = {
  availability: "连通性",
  latency: "响应速度",
  capability: "能力一致性",
  usage: "计量一致性",
  streaming: "流式完整性",
  scheduled: "定时探针",
  manual: "人工复检",
};
