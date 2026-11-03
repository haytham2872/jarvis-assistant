// The orb's look (slice 6): the desktop neural sphere (frontend/src/components/NeuralSphere.jsx)
// ported to one Skia runtime shader. The desktop draws a noise-displaced icosphere with Three.js;
// here each pixel works out where it falls on a sphere seen head-on, so the same ingredients come
// through in 2D: the rim that wobbles with simplex noise, the slowly turning patterned interior, the
// fresnel-bright edge, surface sparkles, a halo that breathes with the voice, and drifting motes.
//
// Uniforms (all floats; colours are 0..1 RGB): resolution, time (s), phase (the rim's noise clock: it
// runs at each state's speed and is accumulated, so a change of speed never makes the rim jump), amp
// (rim wobble), colorA (interior), colorB (edge, glow, motes), level (live voice 0..1), glow (halo
// strength), motes (how many of the 24 motes show, 0..1), radius (the body's radius as a share of the
// box's half-width: small orbs need a bigger body and no motes).
export const ORB_SKSL = `
uniform float2 resolution;
uniform float time;
uniform float amp;
uniform float phase;
uniform float3 colorA;
uniform float3 colorB;
uniform float level;
uniform float glow;
uniform float motes;
uniform float radius;

float3 mod289_3(float3 x) { return x - floor(x * (1.0 / 289.0)) * 289.0; }
float4 mod289_4(float4 x) { return x - floor(x * (1.0 / 289.0)) * 289.0; }
float4 permute(float4 x) { return mod289_4(((x * 34.0) + 1.0) * x); }
float4 taylorInvSqrt(float4 r) { return 1.79284291400159 - 0.85373472095314 * r; }

// Simplex 3D noise (Stefan Gustavson), as on the desktop.
float snoise(float3 v) {
  const float2 C = float2(1.0 / 6.0, 1.0 / 3.0);
  const float4 D = float4(0.0, 0.5, 1.0, 2.0);
  float3 i = floor(v + dot(v, C.yyy));
  float3 x0 = v - i + dot(i, C.xxx);
  float3 g = step(x0.yzx, x0.xyz);
  float3 l = 1.0 - g;
  float3 i1 = min(g.xyz, l.zxy);
  float3 i2 = max(g.xyz, l.zxy);
  float3 x1 = x0 - i1 + C.xxx;
  float3 x2 = x0 - i2 + C.yyy;
  float3 x3 = x0 - D.yyy;
  i = mod289_3(i);
  float4 p = permute(permute(permute(
      i.z + float4(0.0, i1.z, i2.z, 1.0))
    + i.y + float4(0.0, i1.y, i2.y, 1.0))
    + i.x + float4(0.0, i1.x, i2.x, 1.0));
  float n_ = 0.142857142857;
  float3 ns = n_ * D.wyz - D.xzx;
  float4 j = p - 49.0 * floor(p * ns.z * ns.z);
  float4 x_ = floor(j * ns.z);
  float4 y_ = floor(j - 7.0 * x_);
  float4 x = x_ * ns.x + ns.yyyy;
  float4 y = y_ * ns.x + ns.yyyy;
  float4 h = 1.0 - abs(x) - abs(y);
  float4 b0 = float4(x.xy, y.xy);
  float4 b1 = float4(x.zw, y.zw);
  float4 s0 = floor(b0) * 2.0 + 1.0;
  float4 s1 = floor(b1) * 2.0 + 1.0;
  float4 sh = -step(h, float4(0.0));
  float4 a0 = b0.xzyw + s0.xzyw * sh.xxyy;
  float4 a1 = b1.xzyw + s1.xzyw * sh.zzww;
  float3 p0 = float3(a0.xy, h.x);
  float3 p1 = float3(a0.zw, h.y);
  float3 p2 = float3(a1.xy, h.z);
  float3 p3 = float3(a1.zw, h.w);
  float4 norm = taylorInvSqrt(float4(dot(p0, p0), dot(p1, p1), dot(p2, p2), dot(p3, p3)));
  p0 *= norm.x; p1 *= norm.y; p2 *= norm.z; p3 *= norm.w;
  float4 m = max(0.6 - float4(dot(x0, x0), dot(x1, x1), dot(x2, x2), dot(x3, x3)), 0.0);
  m = m * m;
  return 42.0 * dot(m * m, float4(dot(p0, x0), dot(p1, x1), dot(p2, x2), dot(p3, x3)));
}

float hash(float n) { return fract(sin(n * 12.9898) * 43758.5453); }

half4 main(float2 xy) {
  float half_ = 0.5 * min(resolution.x, resolution.y);
  float2 uv = (xy - 0.5 * resolution) / half_;          // -1..1 across the shorter side
  float px = 1.0 / half_;                               // one pixel, in uv
  float R = radius;                                     // the rest is room for the halo
  float t = phase;
  float r = length(uv);
  float2 dir = r > 0.0001 ? uv / r : float2(1.0, 0.0);

  // The rim moves like the desktop sphere's surface, more while he speaks.
  float wob = snoise(float3(dir * 1.1, t * 0.22)) * 0.7 + snoise(float3(dir * 2.3, -t * 0.17)) * 0.3;
  float Rd = R * (1.0 + (amp + level * 0.22) * wob * 0.22);

  float4 col = float4(0.0);
  if (r < Rd + px) {
    float2 q = uv / Rd;
    float z = sqrt(max(0.0, 1.0 - dot(q, q)));
    float3 p = float3(q, z);
    float a = time * 0.08;
    float ca = cos(a);
    float sa = sin(a);
    float3 pr = float3(ca * p.x + sa * p.z, p.y, -sa * p.x + ca * p.z);
    float fres = pow(1.0 - z, 3.5);
    float light = clamp(dot(p, float3(-0.38, -0.46, 0.80)), 0.0, 1.0);
    float n = snoise(pr * 2.2 + time * 0.12) * 0.5 + 0.5;
    float n2 = snoise(pr * 4.8 - time * 0.09) * 0.5 + 0.5;
    float pattern = n * 0.65 + n2 * 0.35;
    // Filaments: the ridges of a slowly moving noise field, like the desktop's synapses.
    float ridge = pow(1.0 - abs(snoise(pr * 2.0 + float3(0.0, 0.0, t * 0.35))), 8.0);
    float3 body = colorA * (0.16 + 0.62 * pattern) * (0.5 + 0.7 * light);
    float3 c3 = body + colorB * ridge * (0.32 + level * 0.45);
    c3 = mix(c3, colorB * (0.75 + fres * 0.7), fres);
    float spark = pow(max(0.0, snoise(pr * 9.0 + time * 0.7)), 3.0);
    c3 += colorB * spark * (0.25 + fres) * 0.5;
    c3 += float3(1.0) * pow(light, 14.0) * 0.10;        // a faint highlight, top left
    float alpha = clamp(0.62 + fres * 0.38, 0.0, 1.0);
    alpha *= smoothstep(Rd + px, Rd - px, r);           // a clean edge at any size
    col = float4(c3 * alpha, alpha);
  }

  // The halo: close and soft at rest, wider and brighter with the voice.
  float d = max(0.0, r - Rd);
  float g = exp(-d * (11.0 - level * 4.5) / R * 0.6) * (glow + level * 0.55);
  col.rgb += colorB * g * 0.6 * (1.0 - col.a);
  col.a += g * 0.6 * (1.0 - col.a);

  // Drifting motes in a ring around it.
  if (motes > 0.0) {
  for (int i = 0; i < 24; i++) {
    float fi = float(i);
    float shown = step(fi / 24.0 + 0.0001, motes);
    float ang = fi * 2.39996 + time * 0.12 * (0.4 + hash(fi + 3.1));
    float rad = R * 1.18 + 0.22 * hash(fi) + 0.025 * sin(time * 0.8 + fi);
    float2 mp = float2(cos(ang), sin(ang)) * rad;
    float md = length(uv - mp);
    float tw = 0.45 + 0.55 * (0.5 + 0.5 * sin(time * 1.7 + fi * 1.3));
    float m = smoothstep(0.03 + px, 0.0, md) * tw * shown * 0.7;
    col.rgb += colorB * m * (1.0 - col.a * 0.5);
    col.a += m * (1.0 - col.a);
  }
  }
  return half4(clamp(col, 0.0, 1.0));
}
`;
