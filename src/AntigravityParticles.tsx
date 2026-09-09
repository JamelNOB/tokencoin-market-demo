import React, { useEffect, useRef } from 'react';
import * as THREE from 'three';

interface Props {
  className?: string;
}

const vertexShader = `
  uniform float uTime;
  uniform vec2 uRingPos;
  uniform float uPixelRatio;
  attribute float aAngle;
  attribute float aRadius;
  attribute float aSeed;
  varying float vAngle;
  varying float vRadius;
  varying float vSeed;
  varying vec2 vLocalPos;

  void main() {
    vAngle = aAngle;
    vRadius = aRadius;
    vSeed = aSeed;

    // Differential vortex rotation speed
    float rotSpeed = 0.12 / (aRadius + 0.4);
    float angle = aAngle + uTime * rotSpeed;

    // Radial breathing shockwave
    float wave = sin(uTime * 1.8 - aRadius * 5.0 + aSeed * 0.5) * 0.035;
    float r = aRadius + wave;

    vec3 pos = vec3(cos(angle) * r, sin(angle) * r * 0.85, 0.0);

    // Gravitational attractor pull towards mouse / ring pos
    vec2 diff = pos.xy - uRingPos;
    float dist = length(diff);
    float pull = 1.0 - smoothstep(0.0, 1.25, dist);
    pos.xy += normalize(diff) * pull * 0.085;

    vLocalPos = pos.xy;

    vec4 mvPosition = modelViewMatrix * vec4(pos, 1.0);
    gl_Position = projectionMatrix * mvPosition;

    // Particle size dynamically expands at wave crests
    float scale = 0.75 + (wave / 0.035) * 0.45;
    gl_PointSize = (13.0 * scale) * uPixelRatio * (1.0 / -mvPosition.z);
  }
`;

const fragmentShader = `
  uniform float uTime;
  uniform vec2 uRingPos;
  uniform vec3 uColor1;
  uniform vec3 uColor2;
  uniform vec3 uColor3;
  uniform vec3 uColor4;
  varying float vAngle;
  varying float vRadius;
  varying float vSeed;
  varying vec2 vLocalPos;

  vec2 rotate(vec2 v, float a) {
    float s = sin(a);
    float c = cos(a);
    return mat2(c, s, -s, c) * v;
  }

  // Signed Distance Function for Rounded Capsule
  float sdRoundBox(in vec2 p, in vec2 b, in vec4 r) {
    r.xy = (p.x > 0.0) ? r.xy : r.zw;
    r.x  = (p.y > 0.0) ? r.x  : r.y;
    vec2 q = abs(p) - b + r.x;
    return min(max(q.x, q.y), 0.0) + length(max(q, 0.0)) - r.x;
  }

  void main() {
    vec2 uv = gl_PointCoord - vec2(0.5);

    // Tangential angle along circular vortex flow
    float tangentAngle = atan(vLocalPos.y - uRingPos.y, vLocalPos.x - uRingPos.x);

    // Rotate grain along tangent direction
    uv = rotate(uv, -tangentAngle + 1.5707963);

    // Pill capsule dimensions
    float d = sdRoundBox(uv, vec2(0.38, 0.14), vec4(0.12));
    float alpha = 1.0 - smoothstep(0.0, 0.06, d);
    if (alpha < 0.01) discard;

    // Organic 4-color palette interpolation
    float progress = fract(vAngle / 6.283185 + uTime * 0.045 + vSeed * 0.22);
    vec3 col;
    if (progress < 0.33) {
      col = mix(uColor1, uColor2, progress * 3.0);
    } else if (progress < 0.66) {
      col = mix(uColor2, uColor3, (progress - 0.33) * 3.0);
    } else {
      col = mix(uColor3, uColor4, (progress - 0.66) * 3.0);
    }

    gl_FragColor = vec4(col, alpha * 0.90);
  }
`;

export const AntigravityParticles: React.FC<Props> = ({ className = '' }) => {
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    let width = container.clientWidth || window.innerWidth;
    let height = container.clientHeight || window.innerHeight;

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(42, width / height, 0.1, 100);
    camera.position.z = 3.6;

    const renderer = new THREE.WebGLRenderer({
      antialias: true,
      alpha: true,
      powerPreference: 'high-performance',
    });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.setSize(width, height);
    container.appendChild(renderer.domElement);

    // TokenCoin + Google Antigravity Brand Palette
    const palette = {
      color1: new THREE.Color('#ee5a35'), // TokenCoin Coral Accent
      color2: new THREE.Color('#2c64ed'), // Google Tech Blue
      color3: new THREE.Color('#ffcf03'), // Amber Yellow
      color4: new THREE.Color('#247b58'), // Verification Green
    };

    // 12,000+ Particles in Concentric Swirling Vortex
    const count = 12000;
    const positions = new Float32Array(count * 3);
    const angles = new Float32Array(count);
    const radii = new Float32Array(count);
    const seeds = new Float32Array(count);

    for (let i = 0; i < count; i++) {
      const r = 0.22 + Math.pow(Math.random(), 0.72) * 2.25;
      const theta = Math.random() * Math.PI * 2;

      positions[i * 3 + 0] = Math.cos(theta) * r;
      positions[i * 3 + 1] = Math.sin(theta) * r * 0.85; // Slight 3D tilt
      positions[i * 3 + 2] = 0;

      angles[i] = theta;
      radii[i] = r;
      seeds[i] = Math.random();
    }

    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    geometry.setAttribute('aAngle', new THREE.BufferAttribute(angles, 1));
    geometry.setAttribute('aRadius', new THREE.BufferAttribute(radii, 1));
    geometry.setAttribute('aSeed', new THREE.BufferAttribute(seeds, 1));

    const material = new THREE.ShaderMaterial({
      transparent: true,
      depthTest: false,
      uniforms: {
        uTime: { value: 0 },
        uRingPos: { value: new THREE.Vector2(0, 0) },
        uPixelRatio: { value: Math.min(window.devicePixelRatio, 2) },
        uColor1: { value: palette.color1 },
        uColor2: { value: palette.color2 },
        uColor3: { value: palette.color3 },
        uColor4: { value: palette.color4 },
      },
      vertexShader,
      fragmentShader,
    });

    const points = new THREE.Points(geometry, material);
    scene.add(points);

    // Mouse Tracking with Inertia
    const mouse = new THREE.Vector2(0, 0);
    const targetMouse = new THREE.Vector2(0, 0);
    let hasMoved = false;

    const onPointerMove = (e: MouseEvent) => {
      hasMoved = true;
      targetMouse.x = (e.clientX / width) * 2 - 1;
      targetMouse.y = -(e.clientY / height) * 2 + 1;
    };

    window.addEventListener('mousemove', onPointerMove);

    // Resize Observer
    const onResize = () => {
      if (!container) return;
      width = container.clientWidth || window.innerWidth;
      height = container.clientHeight || window.innerHeight;
      camera.aspect = width / height;
      camera.updateProjectionMatrix();
      renderer.setSize(width, height);
      material.uniforms.uPixelRatio.value = Math.min(window.devicePixelRatio, 2);
    };

    window.addEventListener('resize', onResize);

    // Animation Loop
    let animId: number;
    const clock = new THREE.Clock();

    const animate = () => {
      animId = requestAnimationFrame(animate);
      const elapsed = clock.getElapsedTime();
      material.uniforms.uTime.value = elapsed;

      // When idle, gently cruise with harmonic drift
      if (!hasMoved) {
        targetMouse.x = Math.sin(elapsed * 0.65) * 0.35;
        targetMouse.y = Math.cos(elapsed * 0.48) * 0.25;
      }

      mouse.lerp(targetMouse, 0.045);
      material.uniforms.uRingPos.value.copy(mouse);

      renderer.render(scene, camera);
    };

    animate();

    return () => {
      cancelAnimationFrame(animId);
      window.removeEventListener('mousemove', onPointerMove);
      window.removeEventListener('resize', onResize);
      geometry.dispose();
      material.dispose();
      renderer.dispose();
      if (renderer.domElement.parentNode) {
        renderer.domElement.parentNode.removeChild(renderer.domElement);
      }
    };
  }, []);

  return (
    <div
      ref={containerRef}
      className={`antigravity-particles-layer ${className}`}
      style={{
        position: 'absolute',
        top: 0,
        left: 0,
        width: '100%',
        height: '100%',
        overflow: 'hidden',
        pointerEvents: 'none',
        zIndex: 0,
      }}
    />
  );
};
