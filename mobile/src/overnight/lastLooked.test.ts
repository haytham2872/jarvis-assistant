import * as SecureStore from 'expo-secure-store';
import { readLooked, saveLooked } from './lastLooked';

jest.mock('expo-secure-store', () => ({ getItemAsync: jest.fn(), setItemAsync: jest.fn() }));
jest.mock('expo-router', () => ({ useFocusEffect: jest.fn() }));
const get = SecureStore.getItemAsync as jest.Mock, set = SecureStore.setItemAsync as jest.Mock;
beforeEach(() => { get.mockReset(); set.mockReset(); get.mockResolvedValue(null); set.mockResolvedValue(undefined); });

it('keeps scopes separate, uses valid key characters and never stores a token', async () => {
  await saveLooked('https://server|hashed-token|looked', 1792912800);
  expect(set.mock.calls[0][0]).toMatch(/^[\w.]+$/);
  expect(set.mock.calls[0][1]).not.toContain('Bearer');
  expect(await readLooked('https://server|hashed-token|looked')).toBe(1792912800);
  expect(await readLooked('https://other|other-hash|looked')).toBeNull();
  await saveLooked('https://server|hashed-token|looked', 1792912700);
  expect(JSON.parse(set.mock.calls[1][1]).at).toBe(1792912800);
});
it('rejects another scope, logs read/write failures and keeps the session marker', async () => {
  const warn = jest.spyOn(console, 'warn').mockImplementation(() => {});
  get.mockResolvedValue(JSON.stringify({ scope: 'wrong', at: 1792912800 }));
  expect(await readLooked('fresh-scope')).toBeNull(); expect(warn).toHaveBeenCalled();
  set.mockRejectedValue(new Error('fixture write failed'));
  await saveLooked('unsaved-scope', 1792912800);
  expect(await readLooked('unsaved-scope')).toBe(1792912800);
  expect(warn).toHaveBeenCalledWith('Overnight: last-looked time could not be saved', expect.any(Error));
  warn.mockRestore();
});
it('loads a persisted timestamp after a new session', async () => {
  get.mockResolvedValue(JSON.stringify({ scope: 'persisted-new-session', at: 1792912800 }));
  expect(await readLooked('persisted-new-session')).toBe(1792912800);
});
