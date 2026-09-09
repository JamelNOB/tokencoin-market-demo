import React, { useState } from 'react';
import { KeyRound, ShieldCheck, ArrowRight, Terminal, Store, Globe } from 'lucide-react';
import { AntigravityParticles } from './AntigravityParticles';

interface Props {
  onLogin: (apiKey: string, role?: 'admin' | 'seller' | 'guest') => void;
}

export const LoginPage: React.FC<Props> = ({ onLogin }) => {
  const [keyInput, setKeyInput] = useState('sk-tc-live-89f410c2e77b');
  const [isSubmitting, setIsSubmitting] = useState(false);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    setIsSubmitting(true);
    setTimeout(() => {
      onLogin(keyInput, 'admin');
    }, 350);
  };

  const handleQuickLogin = (key: string, role: 'admin' | 'seller' | 'guest') => {
    setKeyInput(key);
    setIsSubmitting(true);
    setTimeout(() => {
      onLogin(key, role);
    }, 250);
  };

  return (
    <div className="login-page-root">
      {/* Google Antigravity 3D WebGL Particle Vortex Background */}
      <AntigravityParticles />

      {/* Top Bar / Quick Bypass */}
      <header className="login-topbar">
        <div className="login-badge">
          <span className="pulse-dot" />
          <span>Gateway v0.2.0 · Localhost:8000</span>
        </div>
        <button
          type="button"
          className="guest-bypass-btn"
          onClick={() => onLogin('guest-key', 'guest')}
        >
          免密直接进入控制台 →
        </button>
      </header>

      {/* Center Auth Card */}
      <main className="login-card-container">
        <div className="login-card">
          {/* Brand Mark */}
          <div className="login-brand-header">
            <div className="login-brand-mark" aria-hidden="true">
              <span />
              <span />
              <span />
            </div>
            <div>
              <h1 className="login-title">TokenCoin</h1>
              <p className="login-subtitle">AI API 智能网关与清算基础设施</p>
            </div>
          </div>

          <p className="login-desc">
            采用<strong>首字返回前无感换源</strong>与两阶段计量扣费状态机，让闲置的官方 AI API 能力稳定进入路由。
          </p>

          <form onSubmit={handleSubmit} className="login-form">
            <div className="input-group">
              <label htmlFor="api-key-input" className="input-label">
                <KeyRound size={14} />
                <span>网关统一接入凭据 (API Key)</span>
              </label>
              <div className="input-wrapper">
                <input
                  id="api-key-input"
                  type="text"
                  className="key-input"
                  value={keyInput}
                  onChange={(e) => setKeyInput(e.target.value)}
                  placeholder="sk-tc-..."
                  required
                />
              </div>
            </div>

            <button type="submit" className="login-submit-btn" disabled={isSubmitting}>
              <span>{isSubmitting ? '正在鉴权接入...' : '接入市场工作台'}</span>
              <ArrowRight size={16} />
            </button>
          </form>

          {/* Quick preset credentials */}
          <div className="quick-presets">
            <span className="presets-label">快捷身份测试：</span>
            <div className="preset-chips">
              <button
                type="button"
                className="preset-chip"
                onClick={() => handleQuickLogin('sk-tc-developer-admin', 'admin')}
              >
                <Terminal size={12} />
                <span>开发者密钥</span>
              </button>
              <button
                type="button"
                className="preset-chip"
                onClick={() => handleQuickLogin('sk-tc-seller-demo', 'seller')}
              >
                <Store size={12} />
                <span>货源卖家</span>
              </button>
              <button
                type="button"
                className="preset-chip"
                onClick={() => handleQuickLogin('sk-tc-guest-readonly', 'guest')}
              >
                <Globe size={12} />
                <span>只读观察员</span>
              </button>
            </div>
          </div>

          <div className="login-footer">
            <div className="security-hint">
              <ShieldCheck size={14} />
              <span>内嵌 Canary 自动探针 · 质量与稳定性实时审计</span>
            </div>
          </div>
        </div>
      </main>
    </div>
  );
};
