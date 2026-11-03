import { renderHook } from '@testing-library/react-native';
import * as Haptics from 'expo-haptics';

import { buzzFor, useOrbHaptics } from './haptics';

jest.mock('expo-haptics', () => ({
  impactAsync: jest.fn(), selectionAsync: jest.fn(), ImpactFeedbackStyle: { Soft: 'soft' },
}));

beforeEach(() => jest.clearAllMocks());

it('buzzes when he starts speaking and when a written answer lands, nothing else', () => {
  expect(buzzFor('thinking', 'speaking')).toBe('speaking');
  expect(buzzFor('idle', 'speaking')).toBe('speaking');
  expect(buzzFor('thinking', 'idle')).toBe('answered');
  expect(buzzFor('transcribing', 'idle')).toBe('answered');
  expect(buzzFor('speaking', 'idle')).toBeNull();          // the voice ending says enough
  expect(buzzFor('idle', 'listening')).toBeNull();         // the recorder already taps
  expect(buzzFor('listening', 'transcribing')).toBeNull();
  expect(buzzFor('idle', 'thinking')).toBeNull();
  expect(buzzFor(undefined, 'speaking')).toBeNull();       // never on first sight
  expect(buzzFor('speaking', 'speaking')).toBeNull();
});

it('the hook taps on a change, and only when its orb is the one on screen', async () => {
  type P = { mode: 'thinking' | 'speaking' | 'idle'; on: boolean };
  const { rerender } = await renderHook(({ mode, on }: P) => useOrbHaptics(mode, on), { initialProps: { mode: 'thinking', on: true } as P });
  expect(Haptics.impactAsync).not.toHaveBeenCalled();
  await rerender({ mode: 'speaking', on: true });
  expect(Haptics.impactAsync).toHaveBeenCalledWith('soft');
  await rerender({ mode: 'thinking', on: false });
  await rerender({ mode: 'idle', on: false });
  expect(Haptics.selectionAsync).not.toHaveBeenCalled();
  await rerender({ mode: 'thinking', on: true });
  await rerender({ mode: 'idle', on: true });
  expect(Haptics.selectionAsync).toHaveBeenCalledTimes(1);
});
