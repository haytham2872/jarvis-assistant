import * as SecureStore from 'expo-secure-store';
import { useFocusEffect } from 'expo-router';
import { useCallback, useState } from 'react';

function keyFor(scope: string) {
  let a = 2166136261, b = 5381;
  for (let i = 0; i < scope.length; i++) { a = Math.imul(a ^ scope.charCodeAt(i), 16777619); b = Math.imul(b, 33) ^ scope.charCodeAt(i); }
  return `jarvis.overnight.${(a >>> 0).toString(16)}${(b >>> 0).toString(16)}`;
}
const memory = new Map<string, number>();
export async function readLooked(scope: string): Promise<number | null> {
  if (memory.has(scope)) return memory.get(scope)!;
  try {
    const raw = await SecureStore.getItemAsync(keyFor(scope));
    if (raw == null) return memory.get(scope) ?? null;
    const saved = JSON.parse(raw);
    if (saved.scope !== scope || !Number.isFinite(saved.at)) throw new Error('Invalid last-looked timestamp');
    // A read started before departure must not replace the newer in-memory timestamp.
    const value = Math.max(memory.get(scope) ?? 0, saved.at);
    memory.set(scope, value); return value;
  } catch (error) { console.warn('Overnight: last-looked time could not be read', error); return null; }
}
export async function saveLooked(scope: string, at: number) {
  if (!Number.isFinite(at)) return;
  const value = Math.max(at, memory.get(scope) ?? 0);
  memory.set(scope, value);
  try { await SecureStore.setItemAsync(keyFor(scope), JSON.stringify({ scope, at: value })); }
  catch (error) { console.warn('Overnight: last-looked time could not be saved', error); }
}
export function useLastLooked(scope: string | null) {
  const [state, setState] = useState<{ scope: string | null; at: number | null; loaded: boolean }>({ scope: null, at: null, loaded: false });
  useFocusEffect(useCallback(() => {
    let active = true;
    if (scope) void readLooked(scope).then((at) => { if (active) setState({ scope, at, loaded: true }); });
    return () => { active = false; };
  }, [scope]));
  return state.scope === scope ? state : { at: null, loaded: !scope };
}
