import React, { useRef, useMemo, useEffect } from 'react'
import { Canvas, useFrame } from '@react-three/fiber'
import { Sparkles } from '@react-three/drei'
import { EffectComposer, Bloom } from '@react-three/postprocessing'
import * as THREE from 'three'
import { useStore } from '../store'
import { MOOD } from '../theme'
import useReducedMotion from '../useReducedMotion'

// ─── Simplex 3D Noise (Stefan Gustavson) ──────────────────────────────────────
const NOISE_GLSL = /* glsl */`
vec3 mod289(vec3 x){return x-floor(x*(1./289.))*289.;}
vec4 mod289(vec4 x){return x-floor(x*(1./289.))*289.;}
vec4 permute(vec4 x){return mod289(((x*34.)+1.)*x);}
vec4 taylorInvSqrt(vec4 r){return 1.79284291400159-0.85373472095314*r;}
float snoise(vec3 v){
  const vec2 C=vec2(1./6.,1./3.);
  const vec4 D=vec4(0.,.5,1.,2.);
  vec3 i=floor(v+dot(v,C.yyy));
  vec3 x0=v-i+dot(i,C.xxx);
  vec3 g=step(x0.yzx,x0.xyz);
  vec3 l=1.-g;
  vec3 i1=min(g.xyz,l.zxy);
  vec3 i2=max(g.xyz,l.zxy);
  vec3 x1=x0-i1+C.xxx;
  vec3 x2=x0-i2+C.yyy;
  vec3 x3=x0-D.yyy;
  i=mod289(i);
  vec4 p=permute(permute(permute(
    i.z+vec4(0.,i1.z,i2.z,1.))
   +i.y+vec4(0.,i1.y,i2.y,1.))
   +i.x+vec4(0.,i1.x,i2.x,1.));
  float n_=.142857142857;
  vec3 ns=n_*D.wyz-D.xzx;
  vec4 j=p-49.*floor(p*ns.z*ns.z);
  vec4 x_=floor(j*ns.z);
  vec4 y_=floor(j-7.*x_);
  vec4 x=x_*ns.x+ns.yyyy;
  vec4 y=y_*ns.x+ns.yyyy;
  vec4 h=1.-abs(x)-abs(y);
  vec4 b0=vec4(x.xy,y.xy);
  vec4 b1=vec4(x.zw,y.zw);
  vec4 s0=floor(b0)*2.+1.;
  vec4 s1=floor(b1)*2.+1.;
  vec4 sh=-step(h,vec4(0.));
  vec4 a0=b0.xzyw+s0.xzyw*sh.xxyy;
  vec4 a1=b1.xzyw+s1.xzyw*sh.zzww;
  vec3 p0=vec3(a0.xy,h.x);
  vec3 p1=vec3(a0.zw,h.y);
  vec3 p2=vec3(a1.xy,h.z);
  vec3 p3=vec3(a1.zw,h.w);
  vec4 norm=taylorInvSqrt(vec4(dot(p0,p0),dot(p1,p1),dot(p2,p2),dot(p3,p3)));
  p0*=norm.x; p1*=norm.y; p2*=norm.z; p3*=norm.w;
  vec4 m=max(.6-vec4(dot(x0,x0),dot(x1,x1),dot(x2,x2),dot(x3,x3)),0.);
  m=m*m;
  return 42.*dot(m*m,vec4(dot(p0,x0),dot(p1,x1),dot(p2,x2),dot(p3,x3)));
}`

// ─── Vertex Shader ─────────────────────────────────────────────────────────────
const vertexShader = /* glsl */`
${NOISE_GLSL}
uniform float uTime;
uniform float uAmplitude;
uniform float uSpeed;
varying vec3 vNormal;
varying vec3 vWorldPos;
varying vec3 vPos;

void main(){
  vec3 p = position;
  float n  = snoise(p * 1.6 + uTime * uSpeed * 0.25);
  float n2 = snoise(p * 3.2 - uTime * uSpeed * 0.18);
  float n3 = snoise(p * 6.0 + uTime * uSpeed * 0.4);
  float disp = (n*0.55 + n2*0.3 + n3*0.15) * uAmplitude;
  p += normal * disp;
  vNormal   = normalize(normalMatrix * normal);
  vWorldPos = (modelMatrix * vec4(p, 1.0)).xyz;
  vPos      = p;
  gl_Position = projectionMatrix * modelViewMatrix * vec4(p, 1.0);
}`

// ─── Fragment Shader ───────────────────────────────────────────────────────────
const fragmentShader = /* glsl */`
${NOISE_GLSL}
uniform float uTime;
uniform vec3  uColorA;
uniform vec3  uColorB;
uniform float uAmplitude;
varying vec3  vNormal;
varying vec3  vWorldPos;
varying vec3  vPos;

void main(){
  vec3 viewDir = normalize(cameraPosition - vWorldPos);
  float fresnel = pow(1.0 - clamp(dot(viewDir, vNormal), 0.0, 1.0), 3.5);

  float n  = snoise(vPos * 2.2 + uTime * 0.12) * 0.5 + 0.5;
  float n2 = snoise(vPos * 4.8 - uTime * 0.09) * 0.5 + 0.5;
  float pattern = n * 0.65 + n2 * 0.35;

  vec3 inner  = mix(uColorA * 0.08, uColorA * 0.45, pattern);
  vec3 edge   = uColorB * (0.7 + fresnel * 0.8);
  vec3 color  = mix(inner, edge, fresnel);

  // Hot-spot sparkle on surface
  float spark = pow(max(0.0, snoise(vPos * 9.0 + uTime * 0.7)), 3.0);
  color += uColorB * spark * fresnel * 0.12;

  float alpha = 0.08 + fresnel * 0.48;
  gl_FragColor = vec4(color, alpha);
}`

// ─── State configs ─────────────────────────────────────────────────────────────
// ca/cb aligned to tokens.css --st-* values (normalized RGB).
// idle #2fb6f0, listening #ffb347, thinking #a06cd5, speaking #dcf3ff
const STATES = {
  idle:      { ca: [0.05, 0.45, 0.72], cb: [0.18, 0.71, 0.94], amp: 0.11, spd: 0.26, spark: '#14506b', sparkSize: 1.2, sparkCount: 50  },
  listening: { ca: [0.55, 0.40, 0.05], cb: [1.00, 0.70, 0.28], amp: 0.24, spd: 0.60, spark: '#9a6520', sparkSize: 1.8, sparkCount: 80  },
  thinking:  { ca: [0.30, 0.10, 0.55], cb: [0.63, 0.42, 0.84], amp: 0.40, spd: 1.00, spark: '#4a3460', sparkSize: 2.2, sparkCount: 120 },
  speaking:  { ca: [0.55, 0.72, 0.82], cb: [0.86, 0.95, 1.00], amp: 0.28, spd: 0.75, spark: '#5b727f', sparkSize: 1.9, sparkCount: 75  },
}

// Per-state emphasis scale — the mount fills the viewport frame; state just
// makes the orb "breathe" subtly via a CSS transform scale (no black margins).
const SPHERE_SCALE = { idle: 1.0, listening: 1.06, thinking: 1.02, speaking: 1.08 }

// Per-state base bloom intensity. Speaking uses a near-white color that blooms
// hard at a fixed intensity and washes out the surface detail — keep it low so
// the sphere stays legible while talking.
const BLOOM_BASE = { idle: 0.42, listening: 0.40, thinking: 0.48, speaking: 0.16 }

// Pre-built THREE.Color targets per state — lerp() reads these instead of
// allocating a new Color object every frame (×3 meshes × 60fps).
const COLOR_TARGETS = Object.fromEntries(
  Object.entries(STATES).map(([k, v]) => [k, {
    ca: new THREE.Color(...v.ca),
    cb: new THREE.Color(...v.cb),
  }])
)
const colorsFor = (s) => COLOR_TARGETS[s] || COLOR_TARGETS.idle

// Pre-built mood hue targets (shared with MoodIndicator via theme.js).
const MOOD_COLORS = Object.fromEntries(
  Object.entries(MOOD).map(([k, v]) => [k, new THREE.Color(...v.rgb)])
)
const moodColorFor = (mood) => (mood && MOOD_COLORS[mood.tone]) || null

// Reusable scratch colors so the per-frame mood blend allocates nothing.
// Safe to share: there is one sphere instance and useFrame callbacks run
// sequentially within a frame, each fully consuming the scratch before yielding.
const _scratchA = new THREE.Color()
const _scratchB = new THREE.Color()

// Mix mood into the state target by up to MOOD_MAX, scaled by mood intensity.
const MOOD_MAX = 0.5
function blendMood(out, base, mood) {
  out.copy(base)
  const mc = moodColorFor(mood)
  if (mc) out.lerp(mc, Math.min((mood.intensity || 0) * MOOD_MAX, MOOD_MAX))
  return out
}

// ─── Orb core mesh ─────────────────────────────────────────────────────────────
function OrbCore({ stateRef, ampRef, moodRef, reduced }) {
  const mesh = useRef()
  const mat  = useRef()

  const uniforms = useMemo(() => ({
    uTime:      { value: 0 },
    uAmplitude: { value: 0.13 },
    uSpeed:     { value: 0.30 },
    uColorA:    { value: new THREE.Color(0, 0.35, 0.80) },
    uColorB:    { value: new THREE.Color(0, 0.75, 1.00) },
  }), [])

  useFrame((_, dt) => {
    const cfg = STATES[stateRef.current] || STATES.idle
    const u   = mat.current.uniforms
    // Voice visibly ripples the surface while speaking; calmer otherwise.
    const ampGain = stateRef.current === 'speaking' ? (reduced ? 0.4 : 1.05) : 0.4
    const amp = cfg.amp + ampRef.current * ampGain

    u.uTime.value      += reduced ? dt * 0.4 : dt
    u.uAmplitude.value += (amp    - u.uAmplitude.value) * Math.min(dt * 3, 1)
    u.uSpeed.value     += (cfg.spd - u.uSpeed.value)    * Math.min(dt * 2, 1)

    const ct = colorsFor(stateRef.current)
    blendMood(_scratchA, ct.ca, moodRef.current)
    blendMood(_scratchB, ct.cb, moodRef.current)
    u.uColorA.value.lerp(_scratchA, Math.min(dt * 2, 1))
    u.uColorB.value.lerp(_scratchB, Math.min(dt * 2, 1))

    const rot = reduced ? 0.12 : 1
    mesh.current.rotation.y += dt * 0.08 * rot
    mesh.current.rotation.x += dt * 0.03 * rot
  })

  return (
    <mesh ref={mesh}>
      <icosahedronGeometry args={[1.15, 28]} />
      <shaderMaterial
        ref={mat}
        vertexShader={vertexShader}
        fragmentShader={fragmentShader}
        uniforms={uniforms}
        transparent
        side={THREE.DoubleSide}
        depthWrite={false}
      />
    </mesh>
  )
}

// ─── Outer wireframe shell ─────────────────────────────────────────────────────
function WireShell({ stateRef, moodRef, reduced }) {
  const mesh = useRef()
  const mat  = useRef()

  useFrame((_, dt) => {
    blendMood(_scratchB, colorsFor(stateRef.current).cb, moodRef.current)
    mat.current.color.lerp(_scratchB, Math.min(dt * 1.5, 1))
    const rot = reduced ? 0.12 : 1
    mesh.current.rotation.y -= dt * 0.04 * rot
    mesh.current.rotation.z += dt * 0.02 * rot
  })

  return (
    <mesh ref={mesh}>
      <icosahedronGeometry args={[1.28, 3]} />
      <meshBasicMaterial ref={mat} color="#00bfff" wireframe transparent opacity={0.08} />
    </mesh>
  )
}

// ─── Particle halo ─────────────────────────────────────────────────────────────
function ParticleHalo({ state, reduced }) {
  const cfg = STATES[state] || STATES.idle
  // Count is held constant so changing state updates size/color/speed as live
  // uniforms instead of rebuilding the particle geometry (which flashed on swap).
  return (
    <Sparkles
      count={90}
      scale={5.5}
      size={cfg.sparkSize}
      speed={reduced ? 0 : 0.35}
      color={cfg.spark}
      opacity={0.75}
      noise={0.5}
    />
  )
}

// ─── Ambient / point lights ────────────────────────────────────────────────────
function Lights({ stateRef, moodRef }) {
  const light = useRef()
  useFrame((_, dt) => {
    blendMood(_scratchB, colorsFor(stateRef.current).cb, moodRef.current)
    light.current.color.lerp(_scratchB, Math.min(dt * 1.5, 1))
  })
  return (
    <>
      <ambientLight intensity={0.08} />
      <pointLight ref={light} position={[3, 4, 3]} intensity={0.4} />
      <pointLight position={[-4, -2, -3]} intensity={0.1} color="#4400aa" />
    </>
  )
}

// ─── Full scene ────────────────────────────────────────────────────────────────
// useFrame MUST live here (inside Canvas, outside EffectComposer).
// EffectComposer children run in a different postprocessing renderer — calling
// useFrame inside them registers into the wrong loop and crashes the GPU process
// in production Electron builds.
function Scene({ stateRef, ampRef, moodRef, reduced }) {
  const state   = useStore(s => s.state)
  const bloomRef = useRef()
  const prevState = useRef(stateRef.current)
  const pulse    = useRef(0)

  useFrame((_, dt) => {
    // Bloom pulse: spike intensity on mode entry, decay back to a per-state base.
    if (bloomRef.current) {
      if (stateRef.current !== prevState.current) {
        if (!reduced && (stateRef.current === 'thinking' || stateRef.current === 'listening')) {
          pulse.current = 0.6
        }
        prevState.current = stateRef.current
      }
      pulse.current = Math.max(0, pulse.current - dt * 1.2)
      const target = (BLOOM_BASE[stateRef.current] ?? 0.42) + pulse.current
      // Ease toward target so the glow doesn't snap when switching states.
      bloomRef.current.intensity += (target - bloomRef.current.intensity) * Math.min(dt * 3, 1)
    }
  })

  return (
    <>
      <Lights stateRef={stateRef} moodRef={moodRef} />
      <OrbCore stateRef={stateRef} ampRef={ampRef} moodRef={moodRef} reduced={reduced} />
      <WireShell stateRef={stateRef} moodRef={moodRef} reduced={reduced} />
      <ParticleHalo state={state} reduced={reduced} />
      <EffectComposer>
        <Bloom
          ref={bloomRef}
          intensity={0.45}
          luminanceThreshold={0.45}
          luminanceSmoothing={0.6}
        />
      </EffectComposer>
    </>
  )
}

// ─── Exported component ────────────────────────────────────────────────────────
export default function NeuralSphere() {
  const state     = useStore(s => s.state)
  const amplitude = useStore(s => s.amplitude)
  const mood      = useStore(s => s.mood)
  const reduced   = useReducedMotion()
  const stateRef  = useRef(state)
  const ampRef    = useRef(amplitude)
  const moodRef   = useRef(mood)
  const mountRef  = useRef(null)

  useEffect(() => { stateRef.current = state }, [state])
  useEffect(() => { ampRef.current  = amplitude }, [amplitude])
  useEffect(() => { moodRef.current = mood }, [mood])

  // Drive per-state emphasis scale on the mount element via CSS custom property.
  useEffect(() => {
    if (!mountRef.current) return
    mountRef.current.style.setProperty('--sc', String(SPHERE_SCALE[state] ?? 1))
  }, [state])

  return (
    // sphere-mount fills the ViewportFrame; per-state scale animates via CSS transition.
    <div
      ref={mountRef}
      className="sphere-mount"
      style={{ '--sc': String(SPHERE_SCALE[state] ?? 1) }}
    >
      <Canvas
        style={{ position: 'absolute', inset: 0 }}
        camera={{ position: [0, 0, 3.1], fov: 58 }}
        gl={{ antialias: true, alpha: true, powerPreference: 'high-performance' }}
        dpr={Math.min(window.devicePixelRatio, 2)}
      >
        <Scene stateRef={stateRef} ampRef={ampRef} moodRef={moodRef} reduced={reduced} />
      </Canvas>
    </div>
  )
}
