import { PLATFORM_FEE_RATE } from "./data";
import type { SellerSupply } from "./types";

export function buyerRate(seller: SellerSupply): number {
  return Math.min(0.98, seller.payoutRate + PLATFORM_FEE_RATE);
}

export function eligibleSellers(
  sellers: SellerSupply[],
  modelId: string,
  requiredBudget = 0,
): SellerSupply[] {
  return sellers
    .filter(
      (seller) =>
        seller.modelId === modelId &&
        seller.online &&
        seller.verified &&
        seller.remainingBudget >= requiredBudget,
    )
    .sort((a, b) => {
      const priceDifference = buyerRate(a) - buyerRate(b);
      if (Math.abs(priceDifference) > 0.001) return priceDifference;
      const reliabilityDifference = b.reliability - a.reliability;
      if (Math.abs(reliabilityDifference) > 0.1) return reliabilityDifference;
      return a.latencyMs - b.latencyMs;
    });
}

export function marketSummary(sellers: SellerSupply[], modelId: string) {
  const active = eligibleSellers(sellers, modelId);
  return {
    active,
    sellerCount: active.length,
    totalSupply: active.reduce((sum, seller) => sum + seller.remainingBudget, 0),
    bestRate: active.length ? buyerRate(active[0]) : null,
    medianLatency: active.length
      ? Math.round(
          [...active]
            .sort((a, b) => a.latencyMs - b.latencyMs)[
              Math.floor(active.length / 2)
            ].latencyMs,
        )
      : null,
  };
}

export function calculateUsage(prompt: string, baseCost: number) {
  const inputTokens = Math.max(74, Math.round(prompt.length * 2.4 + 48));
  const outputTokens = 188 + Math.round(prompt.length * 0.8);
  const officialCost = Math.min(
    1.4,
    Math.max(0.46, baseCost + prompt.length * 0.004),
  );
  return { inputTokens, outputTokens, officialCost };
}

export function formatMoney(value: number, digits = 2) {
  return `¥${value.toFixed(digits)}`;
}

export function formatRate(value: number | null) {
  if (value === null) return "暂无";
  return `${(value * 10).toFixed(1)} 折`;
}
