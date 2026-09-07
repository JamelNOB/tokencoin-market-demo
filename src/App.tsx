import {
  ArrowRight,
  BadgeCheck,
  BarChart3,
  Check,
  ChevronRight,
  CircleAlert,
  CircleCheck,
  Clipboard,
  Code2,
  Coins,
  Copy,
  Gauge,
  KeyRound,
  LoaderCircle,
  Pause,
  Play,
  PlugZap,
  RefreshCcw,
  Route,
  Send,
  ShieldCheck,
  Store,
  WalletCards,
  X,
  Zap,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import {
  PLATFORM_FEE_RATE,
  answerByModel,
  cloneInitialState,
  models,
} from "./data";
import {
  buyerRate,
  calculateUsage,
  eligibleSellers,
  formatMoney,
  formatRate,
  marketSummary,
} from "./market";
import type {
  DemoState,
  ModelMarket,
  RouteEvent,
  SellerSupply,
  TradeRecord,
  View,
} from "./types";

const STORAGE_KEY = "tokencoin-demo-state-v1";
const sleep = (milliseconds: number) =>
  new Promise((resolve) => window.setTimeout(resolve, milliseconds));

type CallStage =
  | "idle"
  | "matching"
  | "switching"
  | "generating"
  | "done"
  | "error";

type ToolId = "codex" | "hermes" | "cursor";

function loadState(): DemoState {
  try {
    const saved = window.localStorage.getItem(STORAGE_KEY);
    if (saved) return JSON.parse(saved) as DemoState;
  } catch {
    // The demo still works when storage is blocked.
  }
  return cloneInitialState();
}

function App() {
  const [view, setView] = useState<View>("buyer");
  const [state, setState] = useState<DemoState>(loadState);
  const [selectedModelId, setSelectedModelId] = useState(models[0].id);
  const [configOpen, setConfigOpen] = useState(false);
  const [selectedTool, setSelectedTool] = useState<ToolId>("codex");
  const [toast, setToast] = useState("");

  useEffect(() => {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  }, [state]);

  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(""), 2400);
    return () => window.clearTimeout(timer);
  }, [toast]);

  function resetDemo() {
    setState(cloneInitialState());
    setSelectedModelId(models[0].id);
    setToast("演示数据已恢复");
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand-lockup">
          <div className="brand-mark" aria-hidden="true">
            <span />
            <span />
            <span />
          </div>
          <div>
            <strong>TokenCoin</strong>
            <small>API 调用能力市场</small>
          </div>
        </div>

        <nav className="primary-nav" aria-label="主要导航">
          <button
            className={view === "buyer" ? "nav-item active" : "nav-item"}
            onClick={() => setView("buyer")}
          >
            <Zap size={18} />
            <span>使用模型</span>
          </button>
          <button
            className={view === "seller" ? "nav-item active" : "nav-item"}
            onClick={() => setView("seller")}
          >
            <Store size={18} />
            <span>提供额度</span>
          </button>
        </nav>

        <div className="market-principle">
          <Route size={18} />
          <p>每次调用都由独立供给方竞争成交，平台只负责撮合与交割。</p>
        </div>

        <button className="reset-button" onClick={resetDemo}>
          <RefreshCcw size={15} />
          重置演示
        </button>
      </aside>

      <main className="main-content">
        <header className="topbar">
          <div>
            <span className="eyebrow">
              {view === "buyer" ? "买家工作台" : "卖家工作台"}
            </span>
            <h1>{view === "buyer" ? "一次配置，按需使用" : "接入余额，按次获得收入"}</h1>
          </div>
          <div className="topbar-actions">
            <span className="demo-badge">
              <span className="live-dot" /> 演示环境
            </span>
            {view === "buyer" && (
              <div className="balance-chip">
                <WalletCards size={17} />
                <span>可用余额</span>
                <strong>{formatMoney(state.buyerBalance)}</strong>
              </div>
            )}
          </div>
        </header>

        {view === "buyer" ? (
          <BuyerWorkspace
            state={state}
            setState={setState}
            selectedModelId={selectedModelId}
            setSelectedModelId={setSelectedModelId}
            onConfigure={(tool) => {
              setSelectedTool(tool);
              setConfigOpen(true);
            }}
          />
        ) : (
          <SellerWorkspace state={state} setState={setState} setToast={setToast} />
        )}
      </main>

      {configOpen && (
        <ConfigModal
          tool={selectedTool}
          setTool={setSelectedTool}
          modelId={selectedModelId}
          onClose={() => setConfigOpen(false)}
          onCopied={() => setToast("演示配置已复制")}
        />
      )}

      {toast && (
        <div className="toast" role="status">
          <CircleCheck size={17} /> {toast}
        </div>
      )}
    </div>
  );
}

interface BuyerWorkspaceProps {
  state: DemoState;
  setState: React.Dispatch<React.SetStateAction<DemoState>>;
  selectedModelId: string;
  setSelectedModelId: (modelId: string) => void;
  onConfigure: (tool: ToolId) => void;
}

function BuyerWorkspace({
  state,
  setState,
  selectedModelId,
  setSelectedModelId,
  onConfigure,
}: BuyerWorkspaceProps) {
  const [prompt, setPrompt] = useState("请用三句话解释什么是边际成本。\n");
  const [simulateFailure, setSimulateFailure] = useState(false);
  const [callStage, setCallStage] = useState<CallStage>("idle");
  const [answer, setAnswer] = useState("");
  const [callError, setCallError] = useState("");
  const [routeEvents, setRouteEvents] = useState<RouteEvent[]>([]);
  const [lastTrade, setLastTrade] = useState<TradeRecord | null>(null);

  const selectedModel = models.find((model) => model.id === selectedModelId)!;
  const selectedSummary = marketSummary(state.sellers, selectedModelId);
  const isBusy = ["matching", "switching", "generating"].includes(callStage);

  async function runDemoCall() {
    if (!prompt.trim() || isBusy) return;
    setAnswer("");
    setCallError("");
    setLastTrade(null);
    setCallStage("matching");

    const usage = calculateUsage(prompt, selectedModel.baseCost);
    const candidates = eligibleSellers(
      state.sellers,
      selectedModelId,
      usage.officialCost,
    );

    setRouteEvents([
      {
        label: "需求进入市场",
        detail: `正在比较 ${candidates.length} 个可用报价`,
        state: "active",
      },
      {
        label: "锁定供给",
        detail: "等待撮合",
        state: "muted",
      },
      {
        label: "完成交割",
        detail: "成功后才扣款",
        state: "muted",
      },
    ]);

    await sleep(650);

    if (!candidates.length) {
      setCallStage("error");
      setCallError("当前没有满足条件的可用供给，本次没有扣费。");
      setRouteEvents((events) => [
        { ...events[0], state: "warning", detail: "没有可用报价" },
        ...events.slice(1),
      ]);
      return;
    }

    let selectedSeller = candidates[0];
    let switched = false;

    setRouteEvents((events) => [
      { ...events[0], state: "done", detail: "已比较价格、健康度与余量" },
      {
        ...events[1],
        state: "active",
        detail: `${selectedSeller.alias} · ${formatRate(buyerRate(selectedSeller))}`,
      },
      events[2],
    ]);

    await sleep(600);

    if (simulateFailure) {
      if (candidates.length < 2) {
        setCallStage("error");
        setCallError("首选供给超时，当前没有备用供给。本次没有扣费。");
        setRouteEvents((events) => [
          events[0],
          { ...events[1], state: "warning", detail: "首选供给超时" },
          events[2],
        ]);
        return;
      }
      switched = true;
      setCallStage("switching");
      setRouteEvents((events) => [
        events[0],
        { ...events[1], state: "warning", detail: "首选供给超时，自动换源" },
        events[2],
      ]);
      await sleep(800);
      selectedSeller = candidates[1];
      setRouteEvents((events) => [
        events[0],
        {
          ...events[1],
          state: "done",
          detail: `已切换至 ${selectedSeller.alias}`,
        },
        { ...events[2], state: "active", detail: "正在流式返回" },
      ]);
    } else {
      setRouteEvents((events) => [
        events[0],
        { ...events[1], state: "done" },
        { ...events[2], state: "active", detail: "正在流式返回" },
      ]);
    }

    const rate = buyerRate(selectedSeller);
    const buyerPaid = usage.officialCost * rate;
    const sellerIncome = usage.officialCost * selectedSeller.payoutRate;
    const platformFee = buyerPaid - sellerIncome;

    if (state.buyerBalance < buyerPaid) {
      setCallStage("error");
      setCallError("余额不足，本次没有扣费。请重置演示后再试。");
      setRouteEvents((events) => [
        events[0],
        events[1],
        { ...events[2], state: "warning", detail: "买家余额不足" },
      ]);
      return;
    }

    setCallStage("generating");
    const fullAnswer = answerByModel[selectedModelId];
    for (let index = 0; index < fullAnswer.length; index += 3) {
      setAnswer(fullAnswer.slice(0, index + 3));
      await sleep(24);
    }

    const trade: TradeRecord = {
      id: `TC-${Date.now().toString().slice(-6)}`,
      time: new Intl.DateTimeFormat("zh-CN", {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      }).format(new Date()),
      modelName: selectedModel.name,
      sellerAlias: selectedSeller.alias,
      officialCost: usage.officialCost,
      buyerPaid,
      sellerIncome,
      platformFee,
      inputTokens: usage.inputTokens,
      outputTokens: usage.outputTokens,
      switched,
    };

    setState((current) => ({
      buyerBalance: current.buyerBalance - buyerPaid,
      sellers: current.sellers.map((seller) =>
        seller.id === selectedSeller.id
          ? {
              ...seller,
              remainingBudget: Math.max(
                0,
                seller.remainingBudget - usage.officialCost,
              ),
              pendingIncome: seller.pendingIncome + sellerIncome,
            }
          : seller,
      ),
      trades: [trade, ...current.trades].slice(0, 12),
    }));
    setLastTrade(trade);
    setCallStage("done");
    setRouteEvents((events) => [
      events[0],
      events[1],
      {
        ...events[2],
        state: "done",
        detail: `${formatMoney(buyerPaid)} 已完成分账`,
      },
    ]);
  }

  return (
    <div className="view-enter">
      <section className="buyer-layout">
        <div className="buyer-primary">
          <div className="section-heading compact-heading">
            <div>
              <span className="section-kicker">实时供给</span>
              <h2>选择模型</h2>
            </div>
            <span className="section-note">价格由卖家报价自动形成</span>
          </div>

          <div className="model-market" role="list" aria-label="模型市场">
            {models.map((model) => {
              const summary = marketSummary(state.sellers, model.id);
              const selected = model.id === selectedModelId;
              return (
                <button
                  key={model.id}
                  className={selected ? "model-row selected" : "model-row"}
                  onClick={() => setSelectedModelId(model.id)}
                  role="listitem"
                >
                  <span
                    className="model-monogram"
                    style={{ backgroundColor: model.color }}
                  >
                    {model.shortName}
                  </span>
                  <span className="model-identity">
                    <strong>{model.name}</strong>
                    <small>{model.capability}</small>
                  </span>
                  <span className="market-cell">
                    <small>活跃供给</small>
                    <strong>{summary.sellerCount} 家</strong>
                  </span>
                  <span className="market-cell desktop-cell">
                    <small>可用余量</small>
                    <strong>{formatMoney(summary.totalSupply, 0)}</strong>
                  </span>
                  <span className="rate-cell">
                    <small>当前价格</small>
                    <strong>{formatRate(summary.bestRate)}</strong>
                  </span>
                  <ChevronRight size={17} className="row-arrow" />
                </button>
              );
            })}
          </div>

          <div className="call-workspace">
            <div className="call-header">
              <div>
                <span className="section-kicker">网页体验区</span>
                <h2>发起一次完整流程演示</h2>
              </div>
              <span className="selected-model-label">
                <span style={{ backgroundColor: selectedModel.color }} />
                {selectedModel.name}
              </span>
            </div>

            <label className="prompt-field">
              <span className="sr-only">输入问题</span>
              <textarea
                value={prompt}
                onChange={(event) => setPrompt(event.target.value)}
                placeholder="输入一个问题，体验自动撮合与分账"
                disabled={isBusy}
              />
            </label>

            <div className="call-actions">
              <label className="failure-toggle">
                <input
                  type="checkbox"
                  checked={simulateFailure}
                  onChange={(event) => setSimulateFailure(event.target.checked)}
                  disabled={isBusy}
                />
                <span className="toggle-track" aria-hidden="true">
                  <span />
                </span>
                演示首选卖家超时
              </label>
              <button
                className="primary-button"
                onClick={runDemoCall}
                disabled={isBusy || !prompt.trim()}
              >
                {isBusy ? (
                  <LoaderCircle className="spin" size={17} />
                ) : (
                  <Send size={17} />
                )}
                {callStage === "matching"
                  ? "正在撮合"
                  : callStage === "switching"
                    ? "正在换源"
                    : callStage === "generating"
                      ? "正在生成"
                      : "开始调用"}
              </button>
            </div>

            {(answer || callError || routeEvents.length > 0) && (
              <div className="call-result" aria-live="polite">
                <div className="route-timeline">
                  {routeEvents.map((event, index) => (
                    <div className={`route-step ${event.state}`} key={event.label}>
                      <span className="route-node">
                        {event.state === "done" ? (
                          <Check size={12} />
                        ) : event.state === "warning" ? (
                          <CircleAlert size={12} />
                        ) : (
                          index + 1
                        )}
                      </span>
                      <div>
                        <strong>{event.label}</strong>
                        <small>{event.detail}</small>
                      </div>
                    </div>
                  ))}
                </div>

                {callError ? (
                  <div className="error-message">
                    <CircleAlert size={18} />
                    {callError}
                  </div>
                ) : (
                  answer && (
                    <div className="answer-block">
                      <span>模型回答</span>
                      <p>{answer}</p>
                      {callStage === "generating" && <i className="typing-cursor" />}
                    </div>
                  )
                )}

                {lastTrade && <SettlementReceipt trade={lastTrade} />}
              </div>
            )}
          </div>
        </div>

        <aside className="buyer-inspector">
          <div className="balance-panel">
            <span className="section-kicker">一个账户余额</span>
            <strong>{formatMoney(state.buyerBalance)}</strong>
            <p>可用于平台内所有模型，无需分别充值。</p>
          </div>

          <div className="market-pulse">
            <div className="pulse-title">
              <span>当前市场</span>
              <span className="healthy-label">
                <span /> 供给正常
              </span>
            </div>
            <div className="pulse-stat">
              <div>
                <small>最优成交价</small>
                <strong>{formatRate(selectedSummary.bestRate)}</strong>
              </div>
              <div>
                <small>可切换供给</small>
                <strong>{selectedSummary.sellerCount} 家</strong>
              </div>
            </div>
            <div className="supply-bars" aria-label="供给报价分布">
              {selectedSummary.active.slice(0, 5).map((seller, index) => (
                <span
                  key={seller.id}
                  style={{
                    width: `${Math.max(32, 92 - index * 13)}%`,
                    opacity: 1 - index * 0.13,
                  }}
                >
                  <i />
                  <b>{formatRate(buyerRate(seller))}</b>
                </span>
              ))}
            </div>
          </div>

          <div className="connect-panel">
            <div className="panel-title-row">
              <div>
                <span className="section-kicker">一次配置</span>
                <h3>在熟悉的工具里使用</h3>
              </div>
              <PlugZap size={20} />
            </div>
            <p>平台在后台换卖家，地址与密钥保持不变。</p>
            <div className="tool-buttons">
              <button onClick={() => onConfigure("codex")}>
                <Code2 size={17} /> Codex <ArrowRight size={15} />
              </button>
              <button onClick={() => onConfigure("hermes")}>
                <Coins size={17} /> Hermes <ArrowRight size={15} />
              </button>
              <button onClick={() => onConfigure("cursor")}>
                <Clipboard size={17} /> Cursor <ArrowRight size={15} />
              </button>
            </div>
          </div>
        </aside>
      </section>

      <TradeHistory trades={state.trades} />
    </div>
  );
}

function SettlementReceipt({ trade }: { trade: TradeRecord }) {
  return (
    <div className="settlement-receipt">
      <div className="receipt-heading">
        <div>
          <BadgeCheck size={18} />
          <strong>交割完成</strong>
        </div>
        <span>{trade.id}</span>
      </div>
      <div className="receipt-flow">
        <div>
          <small>买家实付</small>
          <strong>{formatMoney(trade.buyerPaid)}</strong>
        </div>
        <ArrowRight size={16} />
        <div>
          <small>卖家待结算</small>
          <strong>{formatMoney(trade.sellerIncome)}</strong>
        </div>
        <span className="fee-line">
          平台服务费 {formatMoney(trade.platformFee)}
        </span>
      </div>
      <div className="receipt-meta">
        <span>{trade.sellerAlias}</span>
        <span>
          {trade.inputTokens} 输入 · {trade.outputTokens} 输出
        </span>
        {trade.switched && <span className="switch-note">已自动换源</span>}
      </div>
    </div>
  );
}

function TradeHistory({ trades }: { trades: TradeRecord[] }) {
  return (
    <section className="history-section">
      <div className="section-heading">
        <div>
          <span className="section-kicker">透明交割</span>
          <h2>最近调用</h2>
        </div>
        <span className="section-note">每笔成功调用才产生扣款与卖家收入</span>
      </div>
      {trades.length === 0 ? (
        <div className="empty-history">
          <BarChart3 size={21} />
          <span>完成第一笔演示调用后，交割记录会出现在这里。</span>
        </div>
      ) : (
        <div className="history-table-wrap">
          <table className="history-table">
            <thead>
              <tr>
                <th>时间</th>
                <th>模型</th>
                <th>成交供给</th>
                <th>官方消耗</th>
                <th>买家实付</th>
                <th>卖家收入</th>
                <th>状态</th>
              </tr>
            </thead>
            <tbody>
              {trades.slice(0, 6).map((trade) => (
                <tr key={trade.id}>
                  <td>{trade.time}</td>
                  <td>{trade.modelName}</td>
                  <td>{trade.sellerAlias}</td>
                  <td>{formatMoney(trade.officialCost)}</td>
                  <td>{formatMoney(trade.buyerPaid)}</td>
                  <td>{formatMoney(trade.sellerIncome)}</td>
                  <td>
                    <span className="status-inline">
                      <Check size={12} /> 已交割
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

interface SellerWorkspaceProps {
  state: DemoState;
  setState: React.Dispatch<React.SetStateAction<DemoState>>;
  setToast: (message: string) => void;
}

function SellerWorkspace({ state, setState, setToast }: SellerWorkspaceProps) {
  const [modelId, setModelId] = useState(models[0].id);
  const [apiKey, setApiKey] = useState("");
  const [supplyLimit, setSupplyLimit] = useState(50);
  const [payoutPercent, setPayoutPercent] = useState(74);
  const [verifyStage, setVerifyStage] = useState<
    "idle" | "connecting" | "testing" | "done"
  >("idle");
  const [formError, setFormError] = useState("");

  const selectedModel = models.find((model) => model.id === modelId)!;
  const summaryBefore = marketSummary(state.sellers, modelId);
  const mine = state.sellers.filter((seller) => seller.isMine);
  const pendingIncome = mine.reduce((sum, seller) => sum + seller.pendingIncome, 0);
  const remainingSupply = mine.reduce(
    (sum, seller) => sum + seller.remainingBudget,
    0,
  );
  const buyerPercent = Math.min(98, payoutPercent + PLATFORM_FEE_RATE * 100);

  async function publishSupply(event: React.FormEvent) {
    event.preventDefault();
    if (!apiKey.trim()) {
      setFormError("请填入演示密钥，或点击“使用演示密钥”。");
      return;
    }
    if (supplyLimit <= 0) {
      setFormError("可售上限需要大于0元。");
      return;
    }
    setFormError("");
    setVerifyStage("connecting");
    await sleep(700);
    setVerifyStage("testing");
    await sleep(900);

    const prefix = selectedModel.shortName;
    const id = `mine-${Date.now()}`;
    const newSupply: SellerSupply = {
      id,
      alias: `我的供给 ${prefix}-${id.slice(-3)}`,
      modelId,
      provider: selectedModel.provider,
      remainingBudget: supplyLimit,
      listedBudget: supplyLimit,
      payoutRate: payoutPercent / 100,
      reliability: 100,
      latencyMs: 580,
      online: true,
      verified: true,
      pendingIncome: 0,
      isMine: true,
    };

    setState((current) => ({
      ...current,
      sellers: [...current.sellers, newSupply],
    }));
    setVerifyStage("done");
    setApiKey("");
    setToast("供给已上架，市场价格已重新计算");
    await sleep(1000);
    setVerifyStage("idle");
  }

  function toggleSupply(supplyId: string) {
    setState((current) => ({
      ...current,
      sellers: current.sellers.map((seller) =>
        seller.id === supplyId ? { ...seller, online: !seller.online } : seller,
      ),
    }));
  }

  const predictedRate = Math.min(
    summaryBefore.bestRate ?? 1,
    buyerPercent / 100,
  );

  return (
    <div className="view-enter">
      <section className="seller-overview">
        <div>
          <span>我的活跃供给</span>
          <strong>{mine.filter((seller) => seller.online).length}</strong>
          <small>条报价</small>
        </div>
        <div>
          <span>承诺可售额度</span>
          <strong>{formatMoney(remainingSupply)}</strong>
          <small>官方成本上限</small>
        </div>
        <div>
          <span>待结算收入</span>
          <strong>{formatMoney(pendingIncome)}</strong>
          <small>调用验证后可结算</small>
        </div>
      </section>

      <section className="seller-layout">
        <form className="supply-form" onSubmit={publishSupply}>
          <div className="section-heading compact-heading">
            <div>
              <span className="section-kicker">新增供给</span>
              <h2>接入官方 API 余额</h2>
            </div>
            <ShieldCheck size={22} />
          </div>

          <div className="demo-warning">
            <CircleAlert size={16} />
            这是前端演示环境，请勿填写真实 API 密钥。
          </div>

          <label className="form-field">
            <span>提供哪个模型</span>
            <select value={modelId} onChange={(event) => setModelId(event.target.value)}>
              {models.map((model) => (
                <option value={model.id} key={model.id}>
                  {model.name}
                </option>
              ))}
            </select>
          </label>

          <label className="form-field">
            <span>专用 API 密钥</span>
            <div className="key-input-wrap">
              <KeyRound size={17} />
              <input
                type="password"
                value={apiKey}
                onChange={(event) => setApiKey(event.target.value)}
                placeholder="仅用于验证和完成买家调用"
                autoComplete="off"
              />
              <button
                type="button"
                onClick={() => setApiKey("demo_key_for_preview_only")}
              >
                使用演示密钥
              </button>
            </div>
          </label>

          <div className="form-row">
            <label className="form-field">
              <span>最多提供多少官方余额</span>
              <div className="money-input">
                <b>¥</b>
                <input
                  type="number"
                  min="1"
                  max="10000"
                  value={supplyLimit}
                  onChange={(event) => setSupplyLimit(Number(event.target.value))}
                />
              </div>
            </label>
            <label className="form-field">
              <span>每消耗官方1元，希望到手</span>
              <div className="money-input">
                <b>¥</b>
                <input
                  type="number"
                  min="0.55"
                  max="0.92"
                  step="0.01"
                  value={(payoutPercent / 100).toFixed(2)}
                  onChange={(event) =>
                    setPayoutPercent(Math.round(Number(event.target.value) * 100))
                  }
                />
              </div>
            </label>
          </div>

          <div className="price-preview">
            <div>
              <small>你的报价</small>
              <strong>{(payoutPercent / 10).toFixed(1)} 折</strong>
            </div>
            <span>+</span>
            <div>
              <small>平台服务</small>
              <strong>{(PLATFORM_FEE_RATE * 10).toFixed(1)} 折</strong>
            </div>
            <ArrowRight size={17} />
            <div>
              <small>买家看到</small>
              <strong>{(buyerPercent / 10).toFixed(1)} 折</strong>
            </div>
          </div>

          {formError && <div className="inline-form-error">{formError}</div>}

          <button className="primary-button publish-button" disabled={verifyStage !== "idle"}>
            {verifyStage === "idle" && <PlugZap size={17} />}
            {verifyStage === "connecting" && <LoaderCircle className="spin" size={17} />}
            {verifyStage === "testing" && <Gauge size={17} />}
            {verifyStage === "done" && <Check size={17} />}
            {verifyStage === "idle"
              ? "测试并上架"
              : verifyStage === "connecting"
                ? "正在连接"
                : verifyStage === "testing"
                  ? "正在验证模型与余额"
                  : "上架成功"}
          </button>
        </form>

        <div className="seller-side">
          <div className="impact-panel">
            <span className="section-kicker">上架后的市场影响</span>
            <h2>{selectedModel.name}</h2>
            <p>平台按价格、健康度和余量撮合买家请求。</p>
            <div className="impact-comparison">
              <div>
                <small>上架前最优价</small>
                <strong>{formatRate(summaryBefore.bestRate)}</strong>
              </div>
              <ArrowRight size={18} />
              <div className={predictedRate < (summaryBefore.bestRate ?? 1) ? "improved" : ""}>
                <small>加入你的报价后</small>
                <strong>{formatRate(predictedRate)}</strong>
              </div>
            </div>
            <div className="market-rule">
              <Coins size={18} />
              <span>
                你自主定价。低价且稳定的供给会优先成交，平台不提前买断余额。
              </span>
            </div>
          </div>

          <div className="my-supplies">
            <div className="panel-title-row">
              <div>
                <span className="section-kicker">我的报价</span>
                <h3>供给状态</h3>
              </div>
              <span>{mine.length} 条</span>
            </div>
            {mine.length === 0 ? (
              <div className="empty-supplies">
                <Store size={21} />
                <p>还没有供给。完成左侧表单即可看到价格进入市场。</p>
              </div>
            ) : (
              <div className="supply-list">
                {mine.map((supply) => (
                  <div className="supply-item" key={supply.id}>
                    <div
                      className="supply-logo"
                      style={{
                        backgroundColor:
                          models.find((model) => model.id === supply.modelId)?.color ??
                          "#333",
                      }}
                    >
                      {models.find((model) => model.id === supply.modelId)?.shortName}
                    </div>
                    <div className="supply-info">
                      <strong>
                        {models.find((model) => model.id === supply.modelId)?.name}
                      </strong>
                      <span>
                        剩余 {formatMoney(supply.remainingBudget)} · 买家价{" "}
                        {formatRate(buyerRate(supply))}
                      </span>
                    </div>
                    <button
                      className={supply.online ? "supply-toggle online" : "supply-toggle"}
                      onClick={() => toggleSupply(supply.id)}
                      type="button"
                    >
                      {supply.online ? <Pause size={14} /> : <Play size={14} />}
                      {supply.online ? "暂停" : "恢复"}
                    </button>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </section>

      <section className="how-settlement-works">
        <div className="section-heading">
          <div>
            <span className="section-kicker">按使用结算</span>
            <h2>余额不搬家，调用发生时才成交</h2>
          </div>
        </div>
        <div className="settlement-steps">
          <div>
            <span>01</span>
            <strong>卖家承诺供给</strong>
            <p>设定官方余额上限与自己的报价。</p>
          </div>
          <div>
            <span>02</span>
            <strong>买家成功调用</strong>
            <p>请求实际消耗卖家的官方余额。</p>
          </div>
          <div>
            <span>03</span>
            <strong>收入延迟到账</strong>
            <p>调用与抽检通过后进入可结算余额。</p>
          </div>
        </div>
      </section>
    </div>
  );
}

interface ConfigModalProps {
  tool: ToolId;
  setTool: (tool: ToolId) => void;
  modelId: string;
  onClose: () => void;
  onCopied: () => void;
}

const toolNames: Record<ToolId, string> = {
  codex: "Codex",
  hermes: "Hermes",
  cursor: "Cursor",
};

function ConfigModal({ tool, setTool, modelId, onClose, onCopied }: ConfigModalProps) {
  const snippets: Record<ToolId, string> = {
    codex: `model = "${modelId}"\nmodel_provider = "tokencoin"\n\n[model_providers.tokencoin]\nname = "TokenCoin"\nbase_url = "https://api.tokencoin.demo/v1"\nenv_key = "TOKENCOIN_API_KEY"\nwire_api = "responses"`,
    hermes: `model:\n  default: "${modelId}"\n  provider: "custom"\n  base_url: "https://api.tokencoin.demo/v1"\n  api_key: "tc_demo_••••••••"`,
    cursor: `服务类型    OpenAI Compatible\nBase URL    https://api.tokencoin.demo/v1\nAPI Key     tc_demo_••••••••\nModel       ${modelId}`,
  };

  async function copySnippet() {
    try {
      await navigator.clipboard.writeText(snippets[tool]);
    } catch {
      // Clipboard may be unavailable on an insecure preview origin.
    }
    onCopied();
  }

  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={onClose}>
      <section
        className="config-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="config-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <button className="modal-close" onClick={onClose} aria-label="关闭">
          <X size={19} />
        </button>
        <span className="section-kicker">一次配置</span>
        <h2 id="config-title">接入 {toolNames[tool]}</h2>
        <p>以后平台更换供给方，这份配置保持不变。</p>

        <div className="tool-tabs" role="tablist">
          {(Object.keys(toolNames) as ToolId[]).map((toolId) => (
            <button
              key={toolId}
              className={tool === toolId ? "active" : ""}
              onClick={() => setTool(toolId)}
              role="tab"
              aria-selected={tool === toolId}
            >
              {toolNames[toolId]}
            </button>
          ))}
        </div>

        <div className="config-code">
          <pre>{snippets[tool]}</pre>
          <button onClick={copySnippet}>
            <Copy size={16} /> 复制配置
          </button>
        </div>

        <div className="modal-note">
          <ShieldCheck size={17} />
          <span>当前为产品 Demo，地址和密钥仅用于展示，不会产生真实调用。</span>
        </div>
      </section>
    </div>
  );
}

export default App;
