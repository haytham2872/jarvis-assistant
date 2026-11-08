// The live microphone hook with a fake expo-audio stream and a fake socket: when it says the
// stream is unavailable (the caller then falls back to the recorder), and that playback mode is
// restored whenever the stream stops.
import { act, renderHook } from '@testing-library/react-native';

import { useLiveMic } from './useLiveMic';

const mockSetMode = jest.fn().mockResolvedValue(undefined);
const mockStream = {
  start: jest.fn(), stop: jest.fn(), startFileRecordingAsync: jest.fn(), stopFileRecordingAsync: jest.fn(),
};
let mockOnBuffer: (b: { data: ArrayBuffer; sampleRate: number; channels: number; timestamp: number }) => void = () => undefined;
jest.mock('expo-audio', () => ({
  IOSOutputFormat: { MPEG4AAC: 1 }, AudioQuality: { HIGH: 1 },
  requestRecordingPermissionsAsync: jest.fn().mockResolvedValue({ granted: true }),
  setAudioModeAsync: (...a: unknown[]) => mockSetMode(...a),
  useAudioStream: (o: { onBuffer: typeof mockOnBuffer }) => { mockOnBuffer = o.onBuffer; return { stream: mockStream, isStreaming: false }; },
}));
jest.mock('expo-haptics', () => ({ impactAsync: jest.fn(), ImpactFeedbackStyle: { Medium: 1, Light: 0 } }));
jest.mock('expo-file-system', () => ({ File: jest.fn().mockImplementation(() => ({ delete: jest.fn() })) }));
const mockPush = jest.fn();
const mockSessionEnd = jest.fn();
jest.mock('./liveVoice', () => ({
  int16Level: () => -20,
  LiveVoice: jest.fn().mockImplementation(() => ({ push: mockPush, end: mockSessionEnd, cancel: jest.fn(), result: new Promise(() => undefined) })),
}));

const cfg = { baseUrl: 'https://j.example', token: 't' };
const params = { requestId: 'req-1', mode: 'note' as const, spoken: false };
const buffer = (sampleRate: number) => ({ data: new ArrayBuffer(4), sampleRate, channels: 1, timestamp: 0 });

beforeEach(() => {
  jest.clearAllMocks();
  mockStream.start.mockResolvedValue(undefined);
  mockStream.startFileRecordingAsync.mockResolvedValue(undefined);
  mockStream.stopFileRecordingAsync.mockResolvedValue({ uri: 'file:///live.wav', duration: 2.5 });
});

/** Starts and delivers the first buffer at `rate`. */
async function begin(rate = 16000) {
  const hook = await renderHook(() => useLiveMic(cfg, jest.fn()));
  let started: Awaited<ReturnType<typeof hook.result.current.start>> | undefined;
  await act(async () => {
    const p = hook.result.current.start(params);
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
    mockOnBuffer(buffer(rate));
    started = await p;
  });
  return { hook, started: started! };
}

it('streams buffers to the socket once audio arrives at 16 kHz', async () => {
  const { hook, started } = await begin();
  expect(started.ok).toBe(true);
  expect(mockPush).toHaveBeenCalledTimes(1);
  await act(async () => mockOnBuffer(buffer(16000)));
  expect(mockPush).toHaveBeenCalledTimes(2);
  expect(hook.result.current.active).toBe(true);
  await hook.unmount();                    // clears the 2-minute cap
});

it('is unavailable when the native stream cannot start (Expo Go without the module)', async () => {
  mockStream.start.mockRejectedValue(new Error('no native module'));
  const hook = await renderHook(() => useLiveMic(cfg, jest.fn()));
  let started: unknown;
  await act(async () => { started = await hook.result.current.start(params); });
  expect(started).toEqual({ ok: false, why: 'unavailable' });
  expect(mockSetMode).toHaveBeenLastCalledWith(expect.objectContaining({ allowsRecording: false }));
});

it('is unavailable when the stream does not run at 16 kHz', async () => {
  const { started } = await begin(48000);
  expect(started).toEqual({ ok: false, why: 'unavailable' });
  expect(mockPush).not.toHaveBeenCalled();
  expect(mockStream.stop).toHaveBeenCalled();
});

it('is unavailable when the WAV cannot be written (no fallback file would exist)', async () => {
  mockStream.startFileRecordingAsync.mockRejectedValue(new Error('disk'));
  const hook = await renderHook(() => useLiveMic(cfg, jest.fn()));
  let started: unknown;
  await act(async () => { started = await hook.result.current.start(params); });
  expect(started).toEqual({ ok: false, why: 'unavailable' });
});

it('end() tells the server, then finish() stops the stream, returns the WAV and restores playback', async () => {
  const { hook } = await begin();
  let file: unknown;
  await act(async () => { await hook.result.current.end(); file = await hook.result.current.finish(); });
  expect(mockSessionEnd).toHaveBeenCalled();
  expect(file).toEqual({ uri: 'file:///live.wav', ms: 2500 });
  expect(mockStream.stop).toHaveBeenCalled();
  expect(mockSetMode).toHaveBeenLastCalledWith(expect.objectContaining({ allowsRecording: false, playsInSilentMode: true }));
  expect(hook.result.current.active).toBe(false);
});
