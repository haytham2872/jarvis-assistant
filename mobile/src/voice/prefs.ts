// Voice preferences (M3), kept on the phone next to the pairing settings.
import * as SecureStore from 'expo-secure-store';

export type VoicePrefs = {
  /** Read JARVIS's answers to voice messages aloud as soon as they arrive. */
  speakVoiceReplies: boolean;
  /** Playback speed for the server's voice and the iPhone's. */
  rate: number;
};

export const RATES = [1, 1.25, 1.5] as const;
export const DEFAULT_PREFS: VoicePrefs = { speakVoiceReplies: true, rate: 1 };
const KEY = 'jarvis.voice.v1';

export function parsePrefs(raw: string | null): VoicePrefs {
  if (!raw) return DEFAULT_PREFS;
  try {
    const v = JSON.parse(raw);
    return {
      speakVoiceReplies: typeof v.speakVoiceReplies === 'boolean' ? v.speakVoiceReplies : DEFAULT_PREFS.speakVoiceReplies,
      rate: (RATES as readonly number[]).includes(v.rate) ? v.rate : DEFAULT_PREFS.rate,
    };
  } catch {
    return DEFAULT_PREFS;
  }
}

export async function loadPrefs(): Promise<VoicePrefs> {
  try {
    return parsePrefs(await SecureStore.getItemAsync(KEY));
  } catch {
    return DEFAULT_PREFS;
  }
}

export async function savePrefs(p: VoicePrefs): Promise<void> {
  await SecureStore.setItemAsync(KEY, JSON.stringify(p));
}
