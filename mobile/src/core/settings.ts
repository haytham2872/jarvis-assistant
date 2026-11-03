// Connection settings, kept in the iPhone keychain (expo-secure-store). The token never
// goes into the bundle, a published update or a URL.
import * as SecureStore from 'expo-secure-store';

export type Settings = { baseUrl: string; token: string };

const KEY = 'jarvis.settings.v1';
export const EMPTY: Settings = { baseUrl: '', token: '' };

export function parseSettings(raw: string | null): Settings {
  if (!raw) return EMPTY;
  try {
    const v = JSON.parse(raw);
    return {
      baseUrl: typeof v.baseUrl === 'string' ? v.baseUrl : '',
      token: typeof v.token === 'string' ? v.token : '',
    };
  } catch {
    return EMPTY;
  }
}

export async function loadSettings(): Promise<Settings> {
  try {
    return parseSettings(await SecureStore.getItemAsync(KEY));
  } catch {
    return EMPTY;
  }
}

export async function saveSettings(s: Settings): Promise<void> {
  await SecureStore.setItemAsync(KEY, JSON.stringify(s));
}
