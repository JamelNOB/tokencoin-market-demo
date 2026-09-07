export type View = "market" | "seller";

export type ProviderId = "deepseek" | "kimi" | "minimax" | "glm";

export interface ModelMarket {
  id: string;
  provider: ProviderId;
  name: string;
  shortName: string;
  capability: string;
  color: string;
}

export type GatewayMode = "live" | "offline";
export type PlatformStatus = "ok" | "degraded";
export type SupplyHealthStatus =
  | "healthy"
  | "degraded"
  | "open"
  | "half_open";

export interface GatewayStatus {
  mode: GatewayMode;
  version: string;
  configuredProviders: number;
  supportedModels: string[];
}

export interface InfrastructureSummary {
  totalRequests: number;
  successRate: number;
  pendingCny: number;
  lastProbeAt: string | null;
}

export interface LiveSupply {
  id: string;
  provider: string;
  model: string;
  models: string[];
  healthStatus: SupplyHealthStatus;
  reliability: number;
  latencyMs: number | null;
  consecutiveFailures: number;
  available: boolean;
}

export interface AuditRecord {
  id: string;
  supplyId: string;
  checkedAt: string;
  success: boolean;
  latencyMs: number | null;
  checkType: string;
  score: number;
  detail: string;
}

export interface InfrastructureStatus {
  status: PlatformStatus;
  gateway: GatewayStatus;
  summary: InfrastructureSummary;
  supplies: LiveSupply[];
  recentAudits: AuditRecord[];
}

export type ConnectionPhase = "loading" | "online" | "offline";

export interface InfrastructureConnection {
  phase: ConnectionPhase;
  snapshot: InfrastructureStatus | null;
  error: string | null;
  fetchedAt: string | null;
  lastSuccessAt: string | null;
}

export interface ModelMarketRow {
  id: string;
  name: string;
  shortName: string;
  capability: string;
  color: string;
  provider: string;
  supported: boolean;
  supplyCount: number | null;
  availableCount: number | null;
  reliability: number | null;
  latencyMs: number | null;
  healthStatus: SupplyHealthStatus | "unknown";
}
