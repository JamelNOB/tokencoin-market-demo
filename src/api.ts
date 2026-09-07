import type {
  AuditRecord,
  InfrastructureStatus,
  LiveSupply,
  SupplyHealthStatus,
} from "./types";

const configuredBaseUrl = import.meta.env.VITE_STATUS_API_BASE_URL?.trim();
export const STATUS_API_BASE_URL = configuredBaseUrl || "http://127.0.0.1:8000";
export const STATUS_API_URL = `${STATUS_API_BASE_URL.replace(/\/$/, "")}/api/status`;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function invalidStatusPayload(): never {
  throw new Error("状态接口数据格式不正确");
}

function requireRecord(value: unknown): Record<string, unknown> {
  if (!isRecord(value)) invalidStatusPayload();
  return value;
}

function requireString(value: unknown): string {
  if (typeof value !== "string" || !value.trim()) invalidStatusPayload();
  return value;
}

function requireNumber(
  value: unknown,
  options: { integer?: boolean; maximum?: number } = {},
): number {
  if (
    typeof value !== "number" ||
    !Number.isFinite(value) ||
    value < 0 ||
    (options.integer && !Number.isInteger(value)) ||
    (options.maximum !== undefined && value > options.maximum)
  ) {
    invalidStatusPayload();
  }
  return value;
}

function requireBoolean(value: unknown): boolean {
  if (typeof value !== "boolean") invalidStatusPayload();
  return value;
}

function requireStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) invalidStatusPayload();
  return value.map(requireString);
}

function requireNullableTimestamp(value: unknown): string | null {
  if (value === null) return null;
  return requireString(value);
}

function optionalNumber(value: unknown): number | null {
  if (value === null || value === undefined) return null;
  return requireNumber(value);
}

function normalizeHealthStatus(value: unknown): SupplyHealthStatus {
  if (value === "half-open") return "half_open";
  if (
    value === "healthy" ||
    value === "degraded" ||
    value === "open" ||
    value === "half_open"
  ) {
    return value;
  }
  if (value === "disabled") return "open";
  return invalidStatusPayload();
}

function normalizeSupply(value: unknown): LiveSupply {
  const supply = requireRecord(value);
  const models =
    supply.models === undefined ? [] : requireStringArray(supply.models);
  const model =
    supply.model === undefined
      ? models[0] ?? invalidStatusPayload()
      : requireString(supply.model);
  const normalizedModels = models.length ? models : [model];
  const healthStatus = normalizeHealthStatus(
    supply.healthStatus ?? supply.circuit,
  );
  const enabled =
    supply.enabled === undefined ? undefined : requireBoolean(supply.enabled);
  const suppliedAvailability =
    supply.available === undefined
      ? undefined
      : requireBoolean(supply.available);
  if (enabled === undefined && suppliedAvailability === undefined) {
    invalidStatusPayload();
  }
  const inFlight =
    supply.inFlight === undefined
      ? 0
      : requireNumber(supply.inFlight, { integer: true });
  return {
    id: requireString(supply.id),
    provider: requireString(supply.provider),
    model,
    models: normalizedModels,
    healthStatus,
    reliability: requireNumber(
      supply.reliability ?? supply.successRate,
      { maximum: 100 },
    ),
    latencyMs: optionalNumber(supply.latencyMs),
    consecutiveFailures: requireNumber(supply.consecutiveFailures, {
      integer: true,
    }),
    available:
      suppliedAvailability ??
      (enabled !== false &&
        healthStatus !== "open" &&
        !(healthStatus === "half_open" && inFlight > 0)),
  };
}

function normalizeAudit(value: unknown, index: number): AuditRecord {
  const audit = requireRecord(value);
  const status =
    audit.status === undefined ? undefined : requireString(audit.status);
  let success: boolean;
  if (audit.success !== undefined) {
    success = requireBoolean(audit.success);
  } else if (audit.passed !== undefined) {
    success = requireBoolean(audit.passed);
  } else if (status) {
    const normalizedStatus = status.toLowerCase();
    if (["success", "passed", "ok"].includes(normalizedStatus)) {
      success = true;
    } else if (["failure", "failed", "error"].includes(normalizedStatus)) {
      success = false;
    } else {
      invalidStatusPayload();
    }
  } else {
    invalidStatusPayload();
  }
  const detailValue = audit.detail ?? audit.reason ?? status;
  return {
    id:
      audit.id === undefined
        ? `unknown-audit-${index + 1}`
        : requireString(audit.id),
    supplyId: requireString(audit.supplyId),
    checkedAt: requireString(audit.checkedAt),
    success,
    latencyMs: optionalNumber(audit.latencyMs),
    checkType: requireString(audit.checkType ?? audit.auditType),
    score: requireNumber(audit.score, { maximum: 100 }),
    detail:
      detailValue === undefined
        ? "未提供检测说明"
        : requireString(detailValue),
  };
}

export function normalizeInfrastructureStatus(payload: unknown): InfrastructureStatus {
  const root = requireRecord(payload);
  const gateway = requireRecord(root.gateway);
  const summary = requireRecord(root.summary);
  const status = root.status;
  const mode = gateway.mode;
  if (status !== "ok" && status !== "degraded" && status !== "unconfigured") {
    invalidStatusPayload();
  }
  if (mode !== "live" && mode !== "offline" && mode !== "unconfigured") {
    invalidStatusPayload();
  }
  if (!Array.isArray(root.supplies) || !Array.isArray(root.recentAudits)) {
    invalidStatusPayload();
  }

  return {
    status: status === "ok" ? "ok" : "degraded",
    gateway: {
      mode: mode === "live" ? "live" : "offline",
      version: requireString(gateway.version),
      configuredProviders: requireNumber(gateway.configuredProviders, {
        integer: true,
      }),
      supportedModels: requireStringArray(gateway.supportedModels),
    },
    summary: {
      totalRequests: requireNumber(summary.totalRequests, { integer: true }),
      successRate: requireNumber(summary.successRate, { maximum: 100 }),
      pendingCny: requireNumber(summary.pendingCny),
      lastProbeAt: requireNullableTimestamp(summary.lastProbeAt),
    },
    supplies: root.supplies.map(normalizeSupply),
    recentAudits: root.recentAudits.map(normalizeAudit),
  };
}

export async function fetchInfrastructureStatus(
  signal?: AbortSignal,
): Promise<InfrastructureStatus> {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 5000);
  const forwardAbort = () => controller.abort();
  signal?.addEventListener("abort", forwardAbort, { once: true });

  try {
    const response = await fetch(STATUS_API_URL, {
      method: "GET",
      headers: { Accept: "application/json" },
      cache: "no-store",
      signal: controller.signal,
    });
    if (!response.ok) {
      throw new Error(`状态接口返回 HTTP ${response.status}`);
    }
    let payload: unknown;
    try {
      payload = await response.json();
    } catch {
      throw new Error("状态接口数据格式不正确");
    }
    return normalizeInfrastructureStatus(payload);
  } catch (error) {
    if (controller.signal.aborted && !signal?.aborted) {
      throw new Error("状态接口连接超时");
    }
    if (error instanceof Error && error.message.startsWith("状态接口")) {
      throw error;
    }
    throw new Error("状态服务未启动或当前无法访问");
  } finally {
    window.clearTimeout(timeout);
    signal?.removeEventListener("abort", forwardAbort);
  }
}
