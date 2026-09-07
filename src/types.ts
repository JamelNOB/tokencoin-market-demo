export type View = "buyer" | "seller";

export type ProviderId = "deepseek" | "kimi" | "minimax" | "glm";

export interface ModelMarket {
  id: string;
  provider: ProviderId;
  name: string;
  shortName: string;
  capability: string;
  color: string;
  baseCost: number;
}

export interface SellerSupply {
  id: string;
  alias: string;
  modelId: string;
  provider: ProviderId;
  remainingBudget: number;
  listedBudget: number;
  payoutRate: number;
  reliability: number;
  latencyMs: number;
  online: boolean;
  verified: boolean;
  pendingIncome: number;
  isMine?: boolean;
}

export interface RouteEvent {
  label: string;
  detail: string;
  state: "done" | "active" | "muted" | "warning";
}

export interface TradeRecord {
  id: string;
  time: string;
  modelName: string;
  sellerAlias: string;
  officialCost: number;
  buyerPaid: number;
  sellerIncome: number;
  platformFee: number;
  inputTokens: number;
  outputTokens: number;
  switched: boolean;
}

export interface DemoState {
  buyerBalance: number;
  sellers: SellerSupply[];
  trades: TradeRecord[];
}
