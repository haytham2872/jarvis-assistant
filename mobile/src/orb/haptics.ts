// What the orb's state changes feel like (slice 6). The recorder already taps when the
// microphone opens and closes, so the orb adds only what nothing else says: a soft tap when
// JARVIS starts speaking, and a light tick when an answer lands without his voice.
import * as Haptics from 'expo-haptics';
import { useEffect, useRef } from 'react';

import type { OrbMode } from '../voice/levels';

export type Buzz = 'speaking' | 'answered' | null;

export function buzzFor(prev: OrbMode | undefined, next: OrbMode): Buzz {
  if (prev === undefined || prev === next) return null;          // never on first sight
  if (next === 'speaking') return 'speaking';
  if (next === 'idle' && (prev === 'thinking' || prev === 'transcribing')) return 'answered';
  return null;
}

/** Only the orb that is on screen buzzes: two orbs (the header and conversation mode) never both do. */
export function useOrbHaptics(mode: OrbMode, enabled: boolean) {
  const prev = useRef<OrbMode | undefined>(undefined);
  useEffect(() => {
    const before = prev.current;
    prev.current = mode;
    if (!enabled) return;
    const b = buzzFor(before, mode);
    if (b === 'speaking') void Promise.resolve(Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Soft)).catch(() => undefined);
    else if (b === 'answered') void Promise.resolve(Haptics.selectionAsync()).catch(() => undefined);
  }, [mode, enabled]);
}
