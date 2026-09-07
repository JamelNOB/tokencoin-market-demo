import {
  Activity,
  BadgeCheck,
  BarChart3,
  CheckCircle2,
  CircleAlert,
  Clock3,
  Database,
  KeyRound,
  LoaderCircle,
  Radar,
  RefreshCcw,
  Server,
  ShieldCheck,
  Store,
  WalletCards,
  Wifi,
  WifiOff,
  XCircle,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { fetchInfrastructureStatus, STATUS_API_URL } from "./api";
import { checkTypeLabels, healthLabels } from "./data";
import {
  buildModelRows,
  formatInteger,
  formatLatency,
  formatMoney,
  formatPercent,
  formatTime,
  onlineSupplyCount,
} from "./market";
import type {
  AuditRecord,
  InfrastructureConnection,
  InfrastructureStatus,
  LiveSupply,
  ModelMarketRow,
  SupplyHealthStatus,
  View,
} from "./types";

const POLL_INTERVAL_MS = 15_000;

const initialConnection: InfrastructureConnection = {
  phase: "loading",
  snapshot: null,
  error: null,
  fetchedAt: null,
  lastSuccessAt: null,
};

function App() {
  const [view, setView] = useState<View>("market");
  const [connection, setConnection] =
    useState<InfrastructureConnection>(initialConnection);
  const [refreshing, setRefreshing] = useState(false);
  const activeRequest = useRef<AbortController | null>(null);

  const refreshStatus = useCallback(async (showSpinner = false) => {
    activeRequest.current?.abort();
    const controller = new AbortController();
    activeRequest.current = controller;
    if (showSpinner) setRefreshing(true);

    try {
      const snapshot = await fetchInfrastructureStatus(controller.signal);
      if (controller.signal.aborted) return;
      const now = new Date().toISOString();
      setConnection({
        phase: "online",
        snapshot,
        error: null,
        fetchedAt: now,
        lastSuccessAt: now,
      });
    } catch (error) {
      if (controller.signal.aborted) return;
      setConnection((current) => ({
        ...current,
        phase: "offline",
        error:
          error instanceof Error ? error.message : "状态服务当前无法访问",
        fetchedAt: new Date().toISOString(),
      }));
    } finally {
      if (activeRequest.current === controller) {
        activeRequest.current = null;
        setRefreshing(false);
      }
    }
  }, []);

  useEffect(() => {
    void refreshStatus();
    const interval = window.setInterval(() => {
      void refreshStatus();
    }, POLL_INTERVAL_MS);

    return () => {
      window.clearInterval(interval);
      activeRequest.current?.abort();
    };
  }, [refreshStatus]);

  const liveSnapshot =
    connection.phase === "online" ? connection.snapshot : null;

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
            <small>市场基础设施技术 Demo</small>
          </div>
        </div>

        <nav className="primary-nav" aria-label="主要导航">
          <button
            className={view === "market" ? "nav-item active" : "nav-item"}
            onClick={() => setView("market")}
          >
            <BarChart3 size={18} />
            <span>市场与状态</span>
          </button>
          <button
            className={view === "seller" ? "nav-item active" : "nav-item"}
            onClick={() => setView("seller")}
          >
            <Store size={18} />
            <span>卖家工作台</span>
          </button>
        </nav>

        <div className="market-principle">
          <ShieldCheck size={18} />
          <p>先看模型、能力、计量和稳定性信号，再决定是否让货源参与路由。</p>
        </div>

        <div className="sidebar-source">
          <span>只读状态源</span>
          <code>localhost:8000</code>
        </div>
      </aside>

      <main className="main-content">
        <header className="topbar">
          <div>
            <span className="eyebrow">
              {view === "market" ? "市场总览" : "供给与结算"}
            </span>
            <h1>
              {view === "market"
                ? "看清每一份已登记货源的运行信号"
                : "稳定供给，校验计量后写入测试账本"}
            </h1>
          </div>
          <div className="topbar-actions">
            <ConnectionBadge connection={connection} />
            <button
              className="refresh-button"
              onClick={() => void refreshStatus(true)}
              disabled={refreshing}
            >
              {refreshing ? (
                <LoaderCircle className="spin" size={15} />
              ) : (
                <RefreshCcw size={15} />
              )}
              刷新
            </button>
          </div>
        </header>

        <StatusNotice connection={connection} />

        {view === "market" ? (
          <MarketWorkspace snapshot={liveSnapshot} connection={connection} />
        ) : (
          <SellerWorkspace snapshot={liveSnapshot} connection={connection} />
        )}
      </main>
    </div>
  );
}

function ConnectionBadge({
  connection,
}: {
  connection: InfrastructureConnection;
}) {
  if (connection.phase === "loading") {
    return (
      <span className="connection-badge loading">
        <LoaderCircle className="spin" size={14} /> 正在读取后端状态
      </span>
    );
  }
  if (connection.phase === "offline") {
    return (
      <span className="connection-badge offline">
        <WifiOff size={14} /> 状态接口离线
      </span>
    );
  }
  return (
    <span className="connection-badge online">
      <Wifi size={14} /> 状态接口在线
    </span>
  );
}

function StatusNotice({
  connection,
}: {
  connection: InfrastructureConnection;
}) {
  if (connection.phase === "loading") {
    return (
      <div className="status-notice loading" role="status">
        <LoaderCircle className="spin" size={18} />
        <div>
          <strong>正在连接基础设施状态服务</strong>
          <span>读取完成前不展示任何推测数据。</span>
        </div>
      </div>
    );
  }

  if (connection.phase === "offline") {
    return (
      <div className="status-notice offline" role="alert">
        <WifiOff size={19} />
        <div>
          <strong>后端状态当前不可用</strong>
          <span>
            {connection.error}。页面中的状态指标以“—”显示，不用演示数据代替。
          </span>
          {connection.lastSuccessAt && (
            <small>上次成功连接：{formatTime(connection.lastSuccessAt)}</small>
          )}
        </div>
        <code>{STATUS_API_URL}</code>
      </div>
    );
  }

  const snapshot = connection.snapshot;
  if (!snapshot) return null;
  if (snapshot.status === "degraded" || snapshot.gateway.mode === "offline") {
    const unconfigured = snapshot.gateway.configuredProviders === 0;
    return (
      <div className="status-notice degraded" role="status">
        <CircleAlert size={19} />
        <div>
          <strong>
            {unconfigured
              ? "状态服务已连接，但尚未配置上游货源"
              : "状态服务已连接，但网关处于降级状态"}
          </strong>
          <span>
            {unconfigured
              ? "当前显示空状态，不使用样例密钥或模拟检测结果填充。"
              : "以下数据来自后端运行接口；受影响货源当前不应参与正常路由。"}
          </span>
        </div>
        <code>v{snapshot.gateway.version}</code>
      </div>
    );
  }
  return null;
}

interface WorkspaceProps {
  snapshot: InfrastructureStatus | null;
  connection: InfrastructureConnection;
}

function MarketWorkspace({ snapshot, connection }: WorkspaceProps) {
  const modelRows = useMemo(() => buildModelRows(snapshot), [snapshot]);
  return (
    <div className="view-enter">
      <InfrastructureOverview snapshot={snapshot} connection={connection} />

      <section className="content-section">
        <div className="section-heading">
          <div>
            <span className="section-kicker">模型市场</span>
            <h2>可路由供给一览</h2>
          </div>
          <p>价格交易接口尚未接入，因此这里不展示模拟价格。</p>
        </div>
        <ModelMarketTable rows={modelRows} online={Boolean(snapshot)} />
      </section>

      <div className="monitor-grid">
        <section className="content-section monitor-section">
          <div className="section-heading compact">
            <div>
              <span className="section-kicker">全量监控</span>
              <h2>货源健康</h2>
            </div>
            <span className="section-meta">
              {snapshot ? `${snapshot.supplies.length} 个供给` : "等待状态接口"}
            </span>
          </div>
          <SupplyHealthTable supplies={snapshot?.supplies ?? null} compact />
        </section>

        <section className="content-section monitor-section audit-column">
          <div className="section-heading compact">
            <div>
              <span className="section-kicker">概率抽检</span>
              <h2>最近检测</h2>
            </div>
            <Radar size={20} />
          </div>
          <AuditList audits={snapshot?.recentAudits ?? null} limit={6} />
        </section>
      </div>
    </div>
  );
}

function InfrastructureOverview({ snapshot, connection }: WorkspaceProps) {
  const gatewayHealthy =
    snapshot?.status === "ok" && snapshot.gateway.mode === "live";
  return (
    <section className="infra-overview">
      <div className="infra-lead">
        <div className={`infra-orbit ${gatewayHealthy ? "healthy" : "offline"}`}>
          {gatewayHealthy ? <Activity size={26} /> : <Server size={26} />}
        </div>
        <div>
          <span className="section-kicker">基础设施实时状态</span>
          <h2>
            {snapshot
              ? gatewayHealthy
                ? "网关正在接收后端运行状态"
                : "网关未进入正常服务状态"
              : "没有可验证的运行数据"}
          </h2>
          <p>
            {snapshot
              ? `状态接口 v${snapshot.gateway.version} · 最近探针 ${formatTime(snapshot.summary.lastProbeAt)}`
              : "后端不可用时，供给、成功率、调用量和结算额均不作估算。"}
          </p>
        </div>
      </div>

      <div className="infra-stats">
        <Metric
          label="已配置货源"
          value={snapshot ? formatInteger(snapshot.gateway.configuredProviders) : "—"}
          detail={snapshot ? "状态接口返回" : "尚未连接"}
        />
        <Metric
          label="当前可路由货源"
          value={formatInteger(onlineSupplyCount(snapshot))}
          detail={snapshot ? `共 ${snapshot.supplies.length} 个登记供给` : "尚未连接"}
        />
        <Metric
          label="本次运行网关请求"
          value={snapshot ? formatInteger(snapshot.summary.totalRequests) : "—"}
          detail="不含前端演示操作"
        />
        <Metric
          label="上游响应完成率"
          value={snapshot ? formatPercent(snapshot.summary.successRate) : "—"}
          detail={
            connection.fetchedAt
              ? `更新于 ${formatTime(connection.fetchedAt)}`
              : "等待首次读取"
          }
        />
      </div>
    </section>
  );
}

function Metric({
  label,
  value,
  detail,
}: {
  label: string;
  value: string;
  detail: string;
}) {
  return (
    <div className="metric">
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{detail}</small>
    </div>
  );
}

function ModelMarketTable({
  rows,
  online,
}: {
  rows: ModelMarketRow[];
  online: boolean;
}) {
  return (
    <div className="model-market" role="table" aria-label="模型市场状态">
      <div className="model-table-head" role="row">
        <span>模型</span>
        <span>网关登记</span>
        <span>可路由／全部</span>
        <span>平均质量分</span>
        <span>中位延迟</span>
        <span>买家价格</span>
      </div>
      {rows.map((row) => (
        <div className="model-row" role="row" key={row.id}>
          <div className="model-identity">
            <span className="model-monogram" style={{ backgroundColor: row.color }}>
              {row.shortName}
            </span>
            <div>
              <strong>{row.name}</strong>
              <small>{row.capability}</small>
            </div>
          </div>
          <div>
            {online ? (
              <StatusPill
                status={row.supported ? row.healthStatus : "unknown"}
                label={row.supported ? "已登记" : "未登记"}
              />
            ) : (
              <span className="muted-value">—</span>
            )}
          </div>
          <strong className="numeric-value">
            {row.availableCount === null
              ? "—"
              : `${row.availableCount} / ${row.supplyCount}`}
          </strong>
          <strong className="numeric-value">{formatPercent(row.reliability)}</strong>
          <strong className="numeric-value">{formatLatency(row.latencyMs)}</strong>
          <span className="pending-value">待交易接口</span>
        </div>
      ))}
    </div>
  );
}

function SellerWorkspace({ snapshot, connection }: WorkspaceProps) {
  const availableSupplies = onlineSupplyCount(snapshot);
  const isolatedSupplies = snapshot
    ? snapshot.supplies.filter(
        (supply) => !supply.available || supply.healthStatus === "open",
      ).length
    : null;

  return (
    <div className="view-enter">
      <section className="seller-overview">
        <Metric
          label="测试账本 pending 总额"
          value={snapshot ? formatMoney(snapshot.summary.pendingCny, 6) : "—"}
          detail="来自只读状态接口"
        />
        <Metric
          label="可参与路由"
          value={formatInteger(availableSupplies)}
          detail="仅统计当前可参与路由的货源"
        />
        <Metric
          label="隔离或不可用"
          value={formatInteger(isolatedSupplies)}
          detail="不会继续接收新请求"
        />
        <Metric
          label="最近抽检"
          value={snapshot ? formatTime(snapshot.summary.lastProbeAt) : "—"}
          detail={
            connection.phase === "online" ? "后端检测时间" : "状态接口离线"
          }
        />
      </section>

      <div className="seller-layout">
        <section className="content-section supply-workspace">
          <div className="section-heading">
            <div>
              <span className="section-kicker">已接入供给</span>
              <h2>健康与熔断状态</h2>
            </div>
            <p>本页只读取状态，不接收或展示任何 API Key。</p>
          </div>
          <SupplyHealthTable supplies={snapshot?.supplies ?? null} />
        </section>

        <aside className="settlement-panel">
          <div className="settlement-icon">
            <WalletCards size={22} />
          </div>
          <span className="section-kicker">测试账本待结算</span>
          <strong className="settlement-total">
            {snapshot ? formatMoney(snapshot.summary.pendingCny, 6) : "—"}
          </strong>
          <p>
            这是本地测试账本汇总额，并非真实资金。个人账单与提现接口尚未接入。
          </p>
          <div className="settlement-rules">
            <div>
              <CheckCircle2 size={16} />
              <span>
                <strong>合法 usage 才计入卖家收益</strong>
                <small>异常 usage 按平台侧输出暂估，卖家计入 0，收益记录自动标记争议</small>
              </span>
            </div>
            <div>
              <Clock3 size={16} />
              <span>
                <strong>收入先进入待结算</strong>
                <small>留出抽检、复核与争议处理窗口</small>
              </span>
            </div>
            <div>
              <ShieldCheck size={16} />
              <span>
                <strong>异常计量立即隔离</strong>
                <small>对应收益自动标记争议；其他质量问题仍需人工复核</small>
              </span>
            </div>
          </div>
        </aside>
      </div>

      <section className="content-section audit-section">
        <div className="section-heading">
          <div>
            <span className="section-kicker">抽检记录</span>
            <h2>每次判断都留下依据</h2>
          </div>
          <p>异常会计入健康状态；后续由定时探针或人工探针继续复检。</p>
        </div>
        <AuditTable audits={snapshot?.recentAudits ?? null} />
      </section>

      <section className="onboarding-standard">
        <div className="section-heading">
          <div>
            <span className="section-kicker">卖家接入标准</span>
            <h2>第一版只做三件事</h2>
          </div>
          <KeyRound size={21} />
        </div>
        <div className="standard-steps">
          <div>
            <span>01</span>
            <strong>记录上游配置</strong>
            <p>保存供给的 API 地址与模型范围，供路由和后续审计使用。</p>
          </div>
          <div>
            <span>02</span>
            <strong>持续检测稳定性与一致性</strong>
            <p>记录成功率、延迟、能力与计量信号，异常时进入熔断。</p>
          </div>
          <div>
            <span>03</span>
            <strong>调用后延迟结算</strong>
            <p>合法 usage 才计入卖家收益；断流按语义输出暂估，异常计量进入争议。</p>
          </div>
        </div>
      </section>
    </div>
  );
}

function SupplyHealthTable({
  supplies,
  compact = false,
}: {
  supplies: LiveSupply[] | null;
  compact?: boolean;
}) {
  if (supplies === null) {
    return (
      <EmptyState
        icon={<WifiOff size={20} />}
        title="状态接口离线"
        detail="无法确认任何货源的实时健康情况。"
      />
    );
  }
  if (!supplies.length) {
    return (
      <EmptyState
        icon={<Database size={20} />}
        title="尚无已登记供给"
        detail="状态服务在线，但目前没有登记的货源。"
      />
    );
  }

  const rows = compact ? supplies.slice(0, 5) : supplies;
  return (
    <div className="table-scroll">
      <table className="data-table supply-table">
        <thead>
          <tr>
            <th>货源</th>
            <th>模型</th>
            <th>状态</th>
            <th>质量分</th>
            <th>延迟</th>
            {!compact && <th>连续失败</th>}
          </tr>
        </thead>
        <tbody>
          {rows.map((supply) => (
            <tr key={supply.id}>
              <td>
                <strong>{supply.id}</strong>
                <small>{supply.provider}</small>
              </td>
              <td>{supply.models.join("、")}</td>
              <td>
                <StatusPill
                  status={supply.healthStatus}
                  label={healthLabels[supply.healthStatus]}
                />
              </td>
              <td>{formatPercent(supply.reliability)}</td>
              <td>{formatLatency(supply.latencyMs)}</td>
              {!compact && <td>{formatInteger(supply.consecutiveFailures)}</td>}
            </tr>
          ))}
        </tbody>
      </table>
      {compact && supplies.length > rows.length && (
        <p className="table-footnote">仅显示最近 {rows.length} 个货源</p>
      )}
    </div>
  );
}

function AuditList({
  audits,
  limit,
}: {
  audits: AuditRecord[] | null;
  limit: number;
}) {
  if (audits === null) {
    return (
      <EmptyState
        icon={<WifiOff size={20} />}
        title="无法读取抽检记录"
        detail="状态接口恢复后自动更新。"
      />
    );
  }
  if (!audits.length) {
    return (
      <EmptyState
        icon={<Radar size={20} />}
        title="尚无抽检记录"
        detail="没有记录就不展示虚构的检测结果。"
      />
    );
  }
  return (
    <div className="audit-list">
      {audits.slice(0, limit).map((audit) => (
        <div className="audit-item" key={audit.id}>
          <span className={`audit-dot ${audit.success ? "success" : "failure"}`}>
            {audit.success ? <BadgeCheck size={13} /> : <XCircle size={13} />}
          </span>
          <div>
            <div className="audit-title">
              <strong>{checkLabel(audit.checkType)}</strong>
              <span>{formatTime(audit.checkedAt)}</span>
            </div>
            <p>{audit.detail}</p>
            <small>
              {audit.supplyId} · {formatLatency(audit.latencyMs)} · 得分 {formatScore(audit.score)}
            </small>
          </div>
        </div>
      ))}
    </div>
  );
}

function AuditTable({ audits }: { audits: AuditRecord[] | null }) {
  if (audits === null) {
    return (
      <EmptyState
        icon={<WifiOff size={20} />}
        title="状态接口离线"
        detail="当前不能验证任何检测结果。"
      />
    );
  }
  if (!audits.length) {
    return (
      <EmptyState
        icon={<Radar size={20} />}
        title="尚未产生抽检记录"
        detail="探针运行后，时间、类型、结果和依据会显示在这里。"
      />
    );
  }
  return (
    <div className="table-scroll">
      <table className="data-table audit-table">
        <thead>
          <tr>
            <th>检测时间</th>
            <th>货源</th>
            <th>检测类型</th>
            <th>结果</th>
            <th>延迟</th>
            <th>得分</th>
            <th>依据</th>
          </tr>
        </thead>
        <tbody>
          {audits.map((audit) => (
            <tr key={audit.id}>
              <td>{formatTime(audit.checkedAt)}</td>
              <td>{audit.supplyId}</td>
              <td>{checkLabel(audit.checkType)}</td>
              <td>
                <span className={audit.success ? "result-success" : "result-failure"}>
                  {audit.success ? "通过" : "异常"}
                </span>
              </td>
              <td>{formatLatency(audit.latencyMs)}</td>
              <td>{formatScore(audit.score)}</td>
              <td className="detail-cell">{audit.detail}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function StatusPill({
  status,
  label,
}: {
  status: SupplyHealthStatus | "unknown";
  label: string;
}) {
  return (
    <span className={`status-pill ${status}`}>
      <span />
      {label}
    </span>
  );
}

function EmptyState({
  icon,
  title,
  detail,
}: {
  icon: React.ReactNode;
  title: string;
  detail: string;
}) {
  return (
    <div className="empty-state">
      {icon}
      <div>
        <strong>{title}</strong>
        <span>{detail}</span>
      </div>
    </div>
  );
}

function checkLabel(value: string) {
  return checkTypeLabels[value] ?? (value || "未分类");
}

function formatScore(value: number) {
  if (!Number.isFinite(value)) return "—";
  return `${Math.round(value)}`;
}

export default App;
