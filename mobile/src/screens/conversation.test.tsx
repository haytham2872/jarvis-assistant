// Conversation mode's screen (slice 5): the loop of listening, sending, hearing the answer and
// listening again, with the recorder and the app's state faked. Kept outside src/app.
import { act, fireEvent, render, screen } from '@testing-library/react-native';

import { ConversationMode } from '../chat/ConversationMode';

const mockRec = {
  start: jest.fn(), finish: jest.fn(), cancel: jest.fn(), active: false, ms: 0, metering: -55 as number | undefined,
};
jest.mock('../voice/useRecorder', () => ({ useRecorder: () => mockRec }));
const mockLive = {
  start: jest.fn(), end: jest.fn(), cancel: jest.fn(), finish: jest.fn(), active: false, heard: '', ms: 0,
  metering: undefined as number | undefined,
};
let mockLiveCap: () => void = () => undefined;
jest.mock('../voice/useLiveMic', () => ({ useLiveMic: (_cfg: unknown, onCap: () => void) => { mockLiveCap = onCap; return mockLive; } }));
jest.mock('expo-keep-awake', () => ({ useKeepAwake: () => undefined }));
jest.mock('react-native-safe-area-context', () => ({ useSafeAreaInsets: () => ({ top: 59, bottom: 34, left: 0, right: 0 }) }));
const mockOrbProps: Record<string, unknown>[] = [];
jest.mock('../orb/NeuralOrb', () => ({ NeuralOrb: (p: Record<string, unknown>) => { mockOrbProps.push(p); return null; } }));

const mockState: { requests: Record<string, { state: string; text?: string | null; answer?: string | null }> } = { requests: {} };
const mockSpeaker = { state: { source: null as string | null, phase: 'idle' as 'idle' | 'loading' | 'speaking' }, stop: jest.fn() };
const mockApp = {
  cfg: { baseUrl: 'https://j.example', token: 't' },
  chat: { state: mockState as typeof mockState & { mood?: unknown }, warmUp: jest.fn(), sendVoiceTurn: jest.fn(), adoptVoiceTurn: jest.fn() },
  speaker: mockSpeaker,
  levels: { push: jest.fn(), subscribe: jest.fn() },
  setRecording: jest.fn(),
};
jest.mock('../state/AppContext', () => ({ useApp: () => mockApp }));

let t = 1_000_000;
let ui: Awaited<ReturnType<typeof render>>;
const onClose = jest.fn();

/** Feed `db` for `ms` at the recorder's 100 ms pace. */
async function level(db: number, ms: number) {
  for (let i = 0; i < ms; i += 100) {
    t += 100;
    jest.setSystemTime(t);
    mockRec.metering = db + (i % 200 === 0 ? 0 : 0.01);      // a new value each sample, as iOS sends
    await ui.rerender(<ConversationMode onClose={onClose} />);
    await act(async () => {});
  }
}

async function utterance() {
  await level(-55, 700);     // the room
  await level(-25, 1200);    // him
  await level(-55, 1600);    // done
}

beforeEach(() => {
  jest.useFakeTimers();
  t = 1_000_000;
  jest.setSystemTime(t);
  jest.clearAllMocks();
  mockState.requests = {};
  mockSpeaker.state = { source: null, phase: 'idle' };
  mockRec.active = false;
  mockRec.metering = -55;
  mockRec.start.mockImplementation(async () => { mockRec.active = true; return null; });
  mockRec.finish.mockImplementation(async () => { mockRec.active = false; return { uri: 'file:///rec.m4a', ms: 2500, bytes: 9000 }; });
  mockRec.cancel.mockImplementation(async () => { mockRec.active = false; });
  mockLive.active = false;
  mockLive.heard = '';
  mockLive.start.mockResolvedValue({ ok: false, why: 'unavailable' });      // the recorder path, unless a test says otherwise
  mockLive.finish.mockResolvedValue({ uri: 'file:///live.wav', ms: 2500 });
  mockLive.cancel.mockImplementation(async () => { mockLive.active = false; });
});

afterEach(() => {
  jest.useRealTimers();
});

async function open() {
  ui = await render(<ConversationMode onClose={onClose} />);
  await act(async () => {});
}

const rerender = async () => {
  await ui.rerender(<ConversationMode onClose={onClose} />);
  await act(async () => {});
};

it('listens, sends when he stops, hears the answer, and listens again', async () => {
  mockApp.chat.sendVoiceTurn.mockResolvedValue({ requestId: 'r1', line: null });
  await open();
  expect(mockRec.start).toHaveBeenCalledTimes(1);
  expect(screen.getByTestId('conversation-state').props.children).toBe('Go ahead');
  await utterance();
  expect(mockApp.chat.sendVoiceTurn).toHaveBeenCalledWith('file:///rec.m4a', 2500, true);
  expect(screen.getByTestId('conversation-state').props.children).toBe('Thinking…');

  mockState.requests.r1 = { state: 'answered', text: 'what is NVDA at', answer: 'NVDA closed at $165.37.' };
  mockSpeaker.state = { source: 'req:r1', phase: 'speaking' };
  await rerender();
  expect(screen.getByTestId('conversation-state').props.children).toBe('Speaking');
  expect(screen.getByText('NVDA closed at $165.37.')).toBeTruthy();
  expect(mockRec.start).toHaveBeenCalledTimes(1);           // never records over his voice

  mockSpeaker.state = { source: null, phase: 'idle' };
  await rerender();
  expect(mockRec.start).toHaveBeenCalledTimes(2);           // the next turn
});

it('live speech: it listens again only once the live speech is idle, not between clips', async () => {
  mockApp.chat.sendVoiceTurn.mockResolvedValue({ requestId: 'r1', line: null });
  await open();
  await utterance();
  mockSpeaker.state = { source: 'req:r1', phase: 'speaking' };      // the first clip, while the reply is still written
  mockState.requests.r1 = { state: 'running', text: 'what is NVDA at' };
  await rerender();
  mockState.requests.r1 = { state: 'answered', text: 'what is NVDA at', answer: 'NVDA closed at $165.37.' };
  mockSpeaker.state = { source: 'req:r1', phase: 'loading' };       // between two clips
  await rerender();
  await act(async () => { jest.advanceTimersByTime(5_000); });
  expect(mockRec.start).toHaveBeenCalledTimes(1);
  mockSpeaker.state = { source: null, phase: 'idle' };              // speech_end and every clip played
  await rerender();
  expect(mockRec.start).toHaveBeenCalledTimes(2);
});

it('"that\'s all" ends it once the answer has been said', async () => {
  mockApp.chat.sendVoiceTurn.mockResolvedValue({ requestId: 'r2', line: null });
  await open();
  await utterance();
  mockState.requests.r2 = { state: 'answered', text: "Thanks, that's all", answer: 'Any time.' };
  mockSpeaker.state = { source: 'req:r2', phase: 'speaking' };
  await rerender();
  mockSpeaker.state = { source: null, phase: 'idle' };
  await rerender();
  expect(mockRec.start).toHaveBeenCalledTimes(1);
  expect(screen.getByTestId('conversation-state').props.children).toBe('Done');
});

it('silence from the start stops listening and says why', async () => {
  await open();
  await level(-55, 8500);
  expect(mockRec.cancel).toHaveBeenCalled();
  expect(mockApp.chat.sendVoiceTurn).not.toHaveBeenCalled();
  expect(screen.getByText(/stopped listening/)).toBeTruthy();
});

it('a failed turn ends the conversation and says so', async () => {
  mockApp.chat.sendVoiceTurn.mockResolvedValue({ requestId: 'r3', line: null });
  await open();
  await utterance();
  mockState.requests.r3 = { state: 'failed', text: 'hm' };
  await rerender();
  expect(screen.getByText(/could not answer that one/)).toBeTruthy();
  expect(mockRec.start).toHaveBeenCalledTimes(1);
});

it('a recording the server heard nothing in is tried again, twice at most', async () => {
  mockApp.chat.sendVoiceTurn.mockResolvedValue({ requestId: 'r4', line: 'I heard nothing in that recording' });
  await open();
  await utterance();
  expect(mockRec.start).toHaveBeenCalledTimes(2);
  await utterance();
  expect(mockRec.start).toHaveBeenCalledTimes(2);
  expect(screen.getByText('I heard nothing in that recording')).toBeTruthy();
});

it('a spoken answer that never starts is not waited for forever', async () => {
  mockApp.chat.sendVoiceTurn.mockResolvedValue({ requestId: 'r5', line: null });
  await open();
  await utterance();
  mockState.requests.r5 = { state: 'answered', text: 'hi', answer: 'Hello.' };
  await rerender();
  expect(mockRec.start).toHaveBeenCalledTimes(1);
  await act(async () => { t += 7000; jest.setSystemTime(t); jest.advanceTimersByTime(7000); });
  await rerender();
  expect(mockRec.start).toHaveBeenCalledTimes(2);
});

it('End stops the recording and closes', async () => {
  await open();
  await fireEvent.press(screen.getByLabelText('End the conversation'));
  await act(async () => {});
  expect(mockRec.cancel).toHaveBeenCalled();
  expect(mockSpeaker.stop).toHaveBeenCalled();
  expect(onClose).toHaveBeenCalled();
});

it('tapping the orb while he speaks cuts him off', async () => {
  mockApp.chat.sendVoiceTurn.mockResolvedValue({ requestId: 'r6', line: null });
  await open();
  await utterance();
  mockState.requests.r6 = { state: 'answered', text: 'tell me', answer: 'A long answer.' };
  mockSpeaker.state = { source: 'req:r6', phase: 'speaking' };
  await rerender();
  mockSpeaker.stop.mockClear();
  await fireEvent.press(screen.getByLabelText('Stop JARVIS and speak'));
  expect(mockSpeaker.stop).toHaveBeenCalled();
});

it('the microphone being off ends it with the way to fix it', async () => {
  mockRec.start.mockResolvedValue('permission');
  await open();
  expect(screen.getByText(/microphone is off for Expo Go/)).toBeTruthy();
});

it('his voice is stopped before the microphone opens', async () => {
  const order: string[] = [];
  mockSpeaker.stop.mockImplementation(() => order.push('stop'));
  mockRec.start.mockImplementation(async () => { order.push('record'); mockRec.active = true; return null; });
  await open();
  expect(order.slice(0, 2)).toEqual(['stop', 'record']);
});

it('the orb fills the screen, tinted by his mood, and is the one that buzzes', async () => {
  mockApp.chat.state.mood = { tone: 'curious', intensity: 0.5 };
  mockOrbProps.length = 0;
  await open();
  const last = mockOrbProps[mockOrbProps.length - 1];
  expect(last.size).toBeGreaterThanOrEqual(160);
  expect(last).toMatchObject({ mood: { tone: 'curious', intensity: 0.5 }, haptics: true, mode: 'listening' });
  delete mockApp.chat.state.mood;
});

// ------------------------------------------------------------------ live voice

type LiveResult = Record<string, unknown>;
let liveParams: Record<string, unknown>[] = [];
let settleLive: (r: LiveResult) => void = () => undefined;
function liveAvailable() {
  liveParams = [];
  mockLive.start.mockImplementation(async (params: Record<string, unknown>) => {
    liveParams.push(params);
    mockLive.active = true;
    return { ok: true, result: new Promise<LiveResult>((resolve) => { settleLive = resolve; }) };
  });
}
const turn = (id: string) => ({ kind: 'turn', requestId: id, text: 'hello', status: { request_id: id, state: 'queued' } });
const settle = async (r: LiveResult) => { await act(async () => { settleLive(r); }); await rerender(); };

it('a live turn opens a conversation-mode session, shows the words as they come, and the server ends the turn', async () => {
  liveAvailable();
  await open();
  expect(mockRec.start).not.toHaveBeenCalled();
  expect(liveParams[0]).toMatchObject({ mode: 'conversation', spoken: true });
  mockLive.heard = 'what is NVDA';
  await rerender();
  expect(screen.getByText('“what is NVDA”')).toBeTruthy();
  expect(screen.getByTestId('conversation-state').props.children).toBe('Listening…');
  const id = liveParams[0].requestId as string;
  await settle(turn(id));
  expect(mockApp.chat.adoptVoiceTurn).toHaveBeenCalledWith(id, { request_id: id, state: 'queued' }, true, undefined, 'file:///live.wav');
  expect(mockApp.chat.sendVoiceTurn).not.toHaveBeenCalled();
  expect(screen.getByTestId('conversation-state').props.children).toBe('Thinking…');

  mockState.requests[id] = { state: 'answered', text: 'what is NVDA', answer: 'NVDA closed at $165.37.' };
  mockSpeaker.state = { source: `req:${id}`, phase: 'speaking' };
  await rerender();
  expect(screen.getByText('NVDA closed at $165.37.')).toBeTruthy();
  mockSpeaker.state = { source: null, phase: 'idle' };
  await rerender();
  expect(mockLive.start).toHaveBeenCalledTimes(2);          // listens again
});

it('the local Vad is not used while live: silence does not end the turn', async () => {
  liveAvailable();
  await open();
  await utterance();
  expect(mockLive.end).not.toHaveBeenCalled();
  expect(mockApp.chat.adoptVoiceTurn).not.toHaveBeenCalled();
});

it('live: the 2-minute cap tells the server to end', async () => {
  liveAvailable();
  await open();
  await act(async () => { mockLiveCap(); });
  expect(mockLive.end).toHaveBeenCalled();
  expect(screen.getByTestId('conversation-state').props.children).toBe('Sending…');
});

it('live: no_speech ends the conversation with the existing message', async () => {
  liveAvailable();
  await open();
  await settle({ kind: 'no_speech' });
  expect(screen.getByText(/No one spoke for a while/)).toBeTruthy();
  expect(mockApp.chat.adoptVoiceTurn).not.toHaveBeenCalled();
});

it('live: empty is a miss, tried again once, then the conversation stops', async () => {
  liveAvailable();
  await open();
  await settle({ kind: 'empty' });
  expect(mockLive.start).toHaveBeenCalledTimes(2);
  await settle({ kind: 'empty' });
  expect(mockLive.start).toHaveBeenCalledTimes(2);
  expect(screen.getByText(/could not hear you/)).toBeTruthy();
});

it('live: a failed socket uploads the WAV under the same request id', async () => {
  liveAvailable();
  mockApp.chat.sendVoiceTurn.mockResolvedValue({ requestId: 'x', line: null });
  await open();
  const id = liveParams[0].requestId as string;
  await settle({ kind: 'failed', reason: 'closed' });
  expect(mockApp.chat.sendVoiceTurn).toHaveBeenCalledWith('file:///live.wav', 2500, true, undefined, id);
  expect(mockApp.chat.adoptVoiceTurn).not.toHaveBeenCalled();
  expect(screen.getByTestId('conversation-state').props.children).toBe('Thinking…');
});

it('live unavailable: this turn and the next use the recorder as before', async () => {
  mockLive.start.mockResolvedValue({ ok: false, why: 'unavailable' });
  mockApp.chat.sendVoiceTurn.mockResolvedValue({ requestId: 'r9', line: null });
  await open();
  expect(mockRec.start).toHaveBeenCalledTimes(1);
  await utterance();
  expect(mockApp.chat.sendVoiceTurn).toHaveBeenCalledWith('file:///rec.m4a', 2500, true);
});

it('live: End cancels the session', async () => {
  liveAvailable();
  await open();
  await fireEvent.press(screen.getByLabelText('End the conversation'));
  await act(async () => {});
  expect(mockLive.cancel).toHaveBeenCalled();
  expect(onClose).toHaveBeenCalled();
});

it('live: the microphone being off ends it with the way to fix it', async () => {
  mockLive.start.mockResolvedValue({ ok: false, why: 'permission' });
  await open();
  expect(screen.getByText(/microphone is off for Expo Go/)).toBeTruthy();
  expect(mockRec.start).not.toHaveBeenCalled();
});
