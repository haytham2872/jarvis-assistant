// What the pairing QR carries (tools/pair_qr.mjs): jarvis-pair:?base=…&token=…
// Scanned inside the app, so it never depends on Expo Go forwarding a deep link (it did
// not, on 14 October: a camera-scanned exp://…/--/pair link never reached the open app).
import type { Settings } from './settings';

export const PAIR_PREFIX = 'jarvis-pair:';
const MIN_TOKEN = 32;

/** The settings a scanned code carries, or why it is not a pairing code. */
export function parsePairing(data: string): { ok: true; settings: Settings } | { ok: false; why: string } {
  const raw = data.trim();
  if (!raw.startsWith(PAIR_PREFIX)) return { ok: false, why: 'not a JARVIS pairing code' };
  const q = new URLSearchParams(raw.slice(PAIR_PREFIX.length).replace(/^\?/, ''));
  const base = (q.get('base') ?? '').trim().replace(/\/+$/, '');
  const token = (q.get('token') ?? '').trim();
  if (!/^https?:\/\/[^/\s]+/i.test(base)) return { ok: false, why: 'the code has no server address' };
  if (token.length < MIN_TOKEN) return { ok: false, why: 'the code has no usable token' };
  return { ok: true, settings: { baseUrl: base, token } };
}

/** "…7Kq2": enough to tell two tokens apart on screen, never enough to use one. */
export function tokenHint(token: string): string {
  return token.length >= 8 ? `…${token.slice(-4)}` : token ? '…' : 'none';
}
