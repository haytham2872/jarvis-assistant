import { Platform } from 'react-native';

// The desktop's design tokens (frontend/src/styles/tokens.css), for native styles.
// Same names, same values, so the two clients read as one instrument.

export const color = {
  bg: '#060a10',
  bgElev: '#0a0f17',
  surface: '#0e1521',
  surface2: '#131c2b',
  surface3: '#1a2433',
  border: '#1c2838',
  borderBright: '#2a3a4f',
  tHi: '#dce6f0',
  tMid: '#8595a8',
  tDim: '#4b5a6e',
  stIdle: '#2fb6f0',
  stListening: '#ffb347',
  stThinking: '#a06cd5',
  stSpeaking: '#dcf3ff',
  ok: '#46c28a',
  err: '#e5604d',
  warn: '#ffb347',
} as const;

export const space = { s1: 4, s2: 8, s3: 12, s4: 16, s5: 24, s6: 32 } as const;
export const radius = { sm: 4, md: 7, lg: 10, pill: 999 } as const;
export const font = { micro: 11, label: 13, meta: 14, body: 15, read: 18, lg: 22 } as const;
export const mono = Platform.select({ ios: 'Menlo', default: 'monospace' });

// Glass and the backdrop it floats over (slice 1, 15 October 2026). Glass over a flat colour
// shows nothing, so the background is a deep blue that falls to the desktop's near-black, with
// a faint JARVIS-blue light from the top where the orb sits.
export const glass = {
  /** Liquid Glass tint per tone (expo-glass-effect tintColor). */
  tint: {
    clear: undefined,
    blue: 'rgba(47,182,240,0.14)',
    accent: 'rgba(47,182,240,0.78)',
    amber: 'rgba(255,179,71,0.30)',
  } as Record<'clear' | 'blue' | 'accent' | 'amber', string | undefined>,
  /** The frosted fallback's fill per tone, where real glass is not available. */
  frost: {
    clear: 'rgba(22,34,50,0.82)',
    blue: 'rgba(18,46,70,0.85)',
    accent: '#2fb6f0',
    amber: 'rgba(80,58,24,0.9)',
  } as Record<'clear' | 'blue' | 'accent' | 'amber', string>,
  edge: 'rgba(150,205,255,0.20)',
} as const;

export const backdrop = {
  colors: ['#0d2136', '#08131f', '#060a10'] as const,
  locations: [0, 0.38, 1] as const,
  glow: ['rgba(47,182,240,0.16)', 'rgba(47,182,240,0)'] as const,
  /** User bubbles: JARVIS blue, quieter than the accent. */
  bubble: 'rgba(47,182,240,0.15)',
  bubbleEdge: 'rgba(47,182,240,0.32)',
} as const;
