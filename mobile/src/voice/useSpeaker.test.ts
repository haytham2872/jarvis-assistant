import { renderHook } from '@testing-library/react-native';

import { createLevelBus } from './levels';
import { useSpeaker } from './useSpeaker';

const listeners: Record<string, (e: any) => void> = {};
const mockPlayer = {
  isAudioSamplingSupported: true,
  setAudioSamplingEnabled: jest.fn(),
  addListener: jest.fn((name: string, fn: (e: any) => void) => {
    listeners[name] = fn;
    return { remove: () => delete listeners[name] };
  }),
  pause: jest.fn(), play: jest.fn(), replace: jest.fn(), setPlaybackRate: jest.fn(),
};
jest.mock('expo-audio', () => ({
  useAudioPlayer: () => mockPlayer,
  setAudioModeAsync: jest.fn().mockResolvedValue(undefined),
}));
jest.mock('../core/audio', () => ({ PLAYER_OPTIONS: {} }));
jest.mock('expo-speech', () => ({ speak: jest.fn(), stop: jest.fn() }));
jest.mock('expo-file-system', () => ({ Directory: jest.fn(), File: jest.fn(), Paths: {} }));

beforeEach(() => jest.clearAllMocks());

it('sends the real output level of the voice to the orb, and silence when it ends', async () => {
  const bus = createLevelBus(0);
  const got: number[] = [];
  bus.subscribe((v) => got.push(v));
  const { unmount } = await renderHook(() => useSpeaker(null, 1, bus));
  expect(mockPlayer.setAudioSamplingEnabled).toHaveBeenCalledWith(true);
  listeners.audioSampleUpdate({ channels: [{ frames: [0.1, -0.1] }], timestamp: 0 });
  listeners.playbackStatusUpdate({ didJustFinish: true });
  expect(got[0]).toBeCloseTo(0.4);
  expect(got[1]).toBe(0);
  await unmount();
  expect(listeners.audioSampleUpdate).toBeUndefined();
});

it('leaves sampling off without an orb, or where the player cannot sample', async () => {
  await renderHook(() => useSpeaker(null, 1));
  expect(mockPlayer.setAudioSamplingEnabled).not.toHaveBeenCalled();
  mockPlayer.isAudioSamplingSupported = false;
  await renderHook(() => useSpeaker(null, 1, createLevelBus()));
  expect(mockPlayer.setAudioSamplingEnabled).not.toHaveBeenCalled();
  mockPlayer.isAudioSamplingSupported = true;
});
