import { models } from "./data";
import type {
  InfrastructureStatus,
  LiveSupply,
  ModelMarketRow,
  SupplyHealthStatus,
} from "./types";

function median(values: number[]): number | null {
  if (!values.length) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const middle = Math.floor(sorted.length / 2);
  if (sorted.length % 2) return sorted[middle];
  return Math.round((sorted[middle - 1] + sorted[middle]) / 2);
}

function normalizeComparable(value: string) {
  return value.toLowerCase().replace(/[^a-z0-9]/g, "");
}

function suppliesForModel(supplies: LiveSupply[], modelId: string, modelName: string) {
  const identifiers = new Set([
    normalizeComparable(modelId),
    normalizeComparable(modelName),
  ]);
  return supplies.filter((supply) =>
    supply.models.some((model) => identifiers.has(normalizeComparable(model))),
  );
}

function aggregateHealth(supplies: LiveSupply[]): SupplyHealthStatus | "unknown" {
  if (!supplies.length) return "unknown";
  if (supplies.some((supply) => supply.available && supply.healthStatus === "healthy")) {
    return "healthy";
  }
  if (supplies.some((supply) => supply.healthStatus === "half_open")) {
    return "half_open";
  }
  if (supplies.some((supply) => supply.healthStatus === "degraded")) {
    return "degraded";
  }
  return "open";
}

export function buildModelRows(
  snapshot: InfrastructureStatus | null,
): ModelMarketRow[] {
  const connected = snapshot?.gateway.mode === "live";
  const knownRows = models.map((model) => {
    const supplies = snapshot
      ? suppliesForModel(snapshot.supplies, model.id, model.name)
      : [];
    const available = supplies.filter((supply) => supply.available);
    const reliabilityValues = supplies.map((supply) => supply.reliability);
    const latencies = available
      .map((supply) => supply.latencyMs)
      .filter((value): value is number => value !== null);
    const supported = Boolean(
      connected &&
        (supplies.length ||
          snapshot?.gateway.supportedModels.some((entry) =>
            [model.id, model.name].some(
              (candidate) =>
                normalizeComparable(candidate) === normalizeComparable(entry),
            ),
          )),
    );

    return {
      ...model,
      provider: model.provider,
      supported,
      supplyCount: connected ? supplies.length : null,
      availableCount: connected ? available.length : null,
      reliability:
        connected && reliabilityValues.length
          ? reliabilityValues.reduce((sum, value) => sum + value, 0) /
            reliabilityValues.length
          : null,
      latencyMs: connected ? median(latencies) : null,
      healthStatus: connected ? aggregateHealth(supplies) : "unknown",
    } satisfies ModelMarketRow;
  });

  const knownIds = new Set(
    models.flatMap((model) => [
      normalizeComparable(model.id),
      normalizeComparable(model.name),
    ]),
  );
  const extraModels = snapshot?.gateway.supportedModels.filter(
    (model) => !knownIds.has(normalizeComparable(model)),
  ) ?? [];

  const extraRows = extraModels.map((modelName, index) => {
    const supplies = snapshot
      ? suppliesForModel(snapshot.supplies, modelName, modelName)
      : [];
    const available = supplies.filter((supply) => supply.available);
    const latencies = available
      .map((supply) => supply.latencyMs)
      .filter((value): value is number => value !== null);
    return {
      id: modelName,
      name: modelName,
      shortName: modelName.slice(0, 2).toUpperCase(),
      capability: "状态服务已登记模型",
      color: ["#356e62", "#7a5b30", "#52648d"][index % 3],
      provider: supplies[0]?.provider ?? "unknown",
      supported: snapshot?.gateway.mode === "live",
      supplyCount: supplies.length,
      availableCount: available.length,
      reliability: supplies.length
        ? supplies.reduce((sum, supply) => sum + supply.reliability, 0) /
          supplies.length
        : null,
      latencyMs: median(latencies),
      healthStatus: aggregateHealth(supplies),
    } satisfies ModelMarketRow;
  });

  return [...knownRows, ...extraRows];
}

export function formatMoney(value: number | null, digits = 2) {
  if (value === null || !Number.isFinite(value)) return "—";
  return `¥${value.toFixed(digits)}`;
}

export function formatPercent(value: number | null, digits = 1) {
  if (value === null || !Number.isFinite(value)) return "—";
  return `${value.toFixed(digits)}%`;
}

export function formatLatency(value: number | null) {
  if (value === null || !Number.isFinite(value)) return "—";
  return `${Math.round(value)} ms`;
}

export function formatInteger(value: number | null) {
  if (value === null || !Number.isFinite(value)) return "—";
  return new Intl.NumberFormat("zh-CN").format(Math.round(value));
}

export function formatTime(value: string | null) {
  if (!value) return "尚无记录";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(date);
}

export function onlineSupplyCount(snapshot: InfrastructureStatus | null) {
  if (!snapshot || snapshot.gateway.mode !== "live") return null;
  return snapshot.supplies.filter((supply) => supply.available).length;
}
