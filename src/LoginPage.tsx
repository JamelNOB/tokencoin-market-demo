import React, { useState } from 'react';
import { AntigravityParticles } from './AntigravityParticles';

interface Props {
  onLogin: (apiKey: string, role?: 'admin' | 'seller' | 'guest') => void;
}

export const LoginPage: React.FC<Props> = ({ onLogin }) => {
  const [keyInput, setKeyInput] = useState('4/0AY0e-TokenCoin-Live-Gateway-974169037036-sk-tc-89f410c2e77b');
  const [isSubmitting, setIsSubmitting] = useState(false);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    setIsSubmitting(true);
    setTimeout(() => {
      onLogin(keyInput, 'admin');
    }, 300);
  };

  const handleQuickRole = (key: string, role: 'admin' | 'seller' | 'guest') => {
    setKeyInput(key);
    setIsSubmitting(true);
    setTimeout(() => {
      onLogin(key, role);
    }, 250);
  };

  return (
    <div className="google-auth-page-root">
      {/* 100% Official Google Antigravity WebGL Particle Vortex */}
      <AntigravityParticles theme="light" />

      {/* Centered Auth Card matching Google Antigravity Layout */}
      <div className="google-auth-content">
        {/* TokenCoin Antigravity Logo Lockup */}
        <div className="google-brand-lockup">
          <svg className="google-logo-icon" viewBox="0 0 56 56" fill="none" xmlns="http://www.w3.org/2000/svg">
            <circle cx="28" cy="28" r="26" fill="#f8f9fa" />
            <path d="M28 6L7 48H18.5L28 29.5L37.5 48H49L28 6Z" fill="#4285F4" />
            <path d="M28 29.5L21 43H35L28 29.5Z" fill="#EA4335" />
            <circle cx="28" cy="18" r="4.5" fill="#FBBC05" />
          </svg>
          <div className="google-brand-text">
            <span className="brand-google">TokenCoin</span>
            <span className="brand-antigravity">Gateway</span>
          </div>
        </div>

        <p className="google-auth-instructions">
          输入统一网关密钥（API Key）完成身份鉴权与路由接入：
        </p>

        <form onSubmit={handleSubmit} className="google-auth-form">
          <div className="google-code-box">
            <input
              type="text"
              className="google-code-input"
              value={keyInput}
              onChange={(e) => setKeyInput(e.target.value)}
              placeholder="4/0AY0e-TokenCoin-sk-tc-..."
              spellCheck={false}
              required
            />
          </div>

          <div className="google-auth-actions">
            <button type="submit" className="google-action-btn" disabled={isSubmitting}>
              {isSubmitting ? '正在接入基础设施...' : '接入市场控制台 (Enter Console)'}
            </button>
          </div>
        </form>

        {/* Quick Credentials Switcher */}
        <div className="google-quick-roles">
          <span className="roles-title">快速调试身份：</span>
          <button
            type="button"
            className="role-pill"
            onClick={() => handleQuickRole('4/0AY0e-DEV-sk-tc-developer-admin-88e3', 'admin')}
          >
            👨‍💻 开发者密钥
          </button>
          <button
            type="button"
            className="role-pill"
            onClick={() => handleQuickRole('4/0AY0e-SELLER-sk-tc-seller-demo-99b2', 'seller')}
          >
            🏪 货源卖家
          </button>
          <button
            type="button"
            className="role-pill"
            onClick={() => handleQuickRole('4/0AY0e-GUEST-readonly', 'guest')}
          >
            🌐 免密只读访客
          </button>
        </div>

        {/* Google Style Footer Links */}
        <div className="google-footer-links">
          <a
            href="#bypass"
            className="google-link"
            onClick={(e) => {
              e.preventDefault();
              onLogin('guest-token', 'guest');
            }}
          >
            免密直接进入
          </a>
          <span className="google-sep">|</span>
          <a
            href="https://github.com/JamelNOB/tokencoin-market-demo"
            target="_blank"
            rel="noreferrer"
            className="google-link"
          >
            GitHub 源码
          </a>
          <span className="google-sep">|</span>
          <span className="google-status-pill">状态探测源 localhost:8000</span>
        </div>
      </div>
    </div>
  );
};
