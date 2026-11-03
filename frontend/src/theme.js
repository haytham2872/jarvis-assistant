// Design token bridge for WebGL (shader uniforms require normalized RGB arrays,
// not CSS vars). Values are hand-aligned to tokens.css --st-* and --mood-* hexes.
// This is the ONLY place hardcoded values are permitted; everything else uses var(--token).

// Mood tones — hex for any remaining DOM references; rgb (normalized 0–1) for GLSL uniforms.
// Aligned to tokens.css --mood-* hexes.
export const MOOD = {
  contemplative: { hex: '#5a8dd6', rgb: [0.353, 0.553, 0.839] },
  curious:       { hex: '#2fb6f0', rgb: [0.184, 0.714, 0.941] },
  melancholic:   { hex: '#7a6cd5', rgb: [0.478, 0.424, 0.835] },
  sardonic:      { hex: '#7d92a6', rgb: [0.490, 0.573, 0.651] },
  engaged:       { hex: '#e89a4f', rgb: [0.910, 0.604, 0.310] },
  restless:      { hex: '#e5604d', rgb: [0.898, 0.376, 0.302] },
}
