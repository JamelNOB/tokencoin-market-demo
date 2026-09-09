import React, { useEffect } from 'react';

declare global {
  interface Window {
    initGoogleAntigravity?: () => void;
  }
}

interface Props {
  theme?: 'light' | 'dark';
}

export const AntigravityParticles: React.FC<Props> = ({ theme = 'light' }) => {
  useEffect(() => {
    const scriptId = 'google-antigravity-module-script';
    let script = document.getElementById(scriptId) as HTMLScriptElement | null;

    if (!script) {
      script = document.createElement('script');
      script.id = scriptId;
      script.type = 'module';
      script.src = '/_astro/MainParticlesComponent.js';
      document.body.appendChild(script);
    } else {
      if (window.initGoogleAntigravity) {
        setTimeout(() => {
          window.initGoogleAntigravity?.();
        }, 50);
      }
    }
  }, []);

  return (
    <div
      className="main-particles-component-section"
      data-main-particles-component
      data-theme={theme}
      data-ring-width="0.011"
      data-ring-width2="0.107"
      data-ring-displacement="0.53"
      data-density="230"
      data-particles-scale="0.59"
    >
      <div className="main-particles-container" data-container />
    </div>
  );
};
