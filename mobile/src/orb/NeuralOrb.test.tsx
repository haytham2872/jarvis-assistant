// Which orb shows: the GPU one only when Skia, Reanimated and Worklets are all in this build and
// the shader compiled; the Animated orb otherwise, and when the GPU one throws.
import { render, screen } from '@testing-library/react-native';
import { Platform, TurboModuleRegistry } from 'react-native';

import { createLevelBus } from '../voice/levels';
import { _resetGpuOrb, gpuOrb, NeuralOrb } from './NeuralOrb';

const mockGpu = { props: [] as Record<string, unknown>[], effect: {} as unknown, throws: false };
jest.mock('./SkiaOrb', () => ({
  get EFFECT() { return mockGpu.effect; },
  SkiaOrb: (p: Record<string, unknown>) => {
    if (mockGpu.throws) throw new Error('GPU lost');
    mockGpu.props.push(p);
    const { Text } = require('react-native');
    return <Text testID="gpu-orb">gpu</Text>;
  },
}));
jest.mock('expo-haptics', () => ({ impactAsync: jest.fn(), selectionAsync: jest.fn(), ImpactFeedbackStyle: { Soft: 'soft' } }));

const bus = createLevelBus();
let present: Set<string>;
let spy: jest.SpyInstance;

beforeEach(() => {
  _resetGpuOrb();
  mockGpu.props = [];
  mockGpu.effect = {};
  mockGpu.throws = false;
  present = new Set(['RNSkiaModule', 'WorkletsModule', 'ReanimatedModule']);
  const real = TurboModuleRegistry.get.bind(TurboModuleRegistry);
  const ours = new Set(['RNSkiaModule', 'WorkletsModule', 'ReanimatedModule']);
  spy = jest.spyOn(TurboModuleRegistry, 'get')
    .mockImplementation((n: string) => (ours.has(n) ? (present.has(n) ? ({} as never) : null) : real(n)));
});
afterEach(() => spy.mockRestore());

it('draws the GPU orb when everything it needs is here, with its size, mood and motion setting', async () => {
  await render(<NeuralOrb mode="thinking" bus={bus} size={40} mood={{ tone: 'curious', intensity: 0.3 }} reduced />);
  expect(screen.getByTestId('gpu-orb')).toBeTruthy();
  expect(mockGpu.props[0]).toMatchObject({ mode: 'thinking', size: 40, mood: { tone: 'curious', intensity: 0.3 }, reduced: true });
  expect(screen.getByLabelText('JARVIS is thinking')).toBeTruthy();
});

it.each(['RNSkiaModule', 'WorkletsModule', 'ReanimatedModule'])('falls back to the Animated orb without %s', async (name) => {
  present.delete(name);
  await render(<NeuralOrb mode="idle" bus={bus} size={40} />);
  expect(screen.queryByTestId('gpu-orb')).toBeNull();
  expect(screen.getByTestId('orb-fallback', { includeHiddenElements: true })).toBeTruthy();
  expect(screen.getByLabelText('JARVIS is idle')).toBeTruthy();
});

it('falls back when the shader did not compile, and on the web', () => {
  mockGpu.effect = null;
  expect(gpuOrb()).toBeNull();
  _resetGpuOrb();
  mockGpu.effect = {};
  const os = Platform.OS;
  Object.defineProperty(Platform, 'OS', { value: 'web', configurable: true });
  try {
    expect(gpuOrb()).toBeNull();
  } finally {
    Object.defineProperty(Platform, 'OS', { value: os, configurable: true });
  }
});

it('a GPU orb that throws while drawing is replaced by the Animated one, not a crash', async () => {
  mockGpu.throws = true;
  const err = jest.spyOn(console, 'error').mockImplementation(() => undefined);
  await render(<NeuralOrb mode="speaking" bus={bus} size={300} />);
  expect(screen.getByTestId('orb-fallback', { includeHiddenElements: true })).toBeTruthy();
  err.mockRestore();
});

it('probes once: a missing module found later does not swap orbs mid-session', () => {
  expect(gpuOrb()).not.toBeNull();
  present.clear();
  expect(gpuOrb()).not.toBeNull();
});

it('VoiceOver hears the orb once: the fallback inside is hidden', async () => {
  present.clear();
  await render(<NeuralOrb mode="listening" bus={bus} size={40} />);
  expect(screen.getAllByLabelText('JARVIS is listening')).toHaveLength(1);
  expect(screen.queryByTestId('orb-fallback')).toBeNull();
});

it('buzzes only when told it is the orb on screen', async () => {
  const Haptics = jest.requireMock('expo-haptics');
  const { rerender } = await render(<NeuralOrb mode="thinking" bus={bus} size={40} />);
  await rerender(<NeuralOrb mode="speaking" bus={bus} size={40} />);
  expect(Haptics.impactAsync).not.toHaveBeenCalled();
  await rerender(<NeuralOrb mode="thinking" bus={bus} size={40} haptics />);
  await rerender(<NeuralOrb mode="speaking" bus={bus} size={40} haptics />);
  expect(Haptics.impactAsync).toHaveBeenCalledTimes(1);
});
