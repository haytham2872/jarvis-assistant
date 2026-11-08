// The phone's half of a voice message: what happens to the request and the recording file
// on each answer the server can give.
import { act, renderHook, waitFor } from '@testing-library/react-native';

import * as client from './client';
import * as batching from './eventBatch';
import type { RequestStatus, Snapshot } from './types';
import { MIN_VOICE_MS, useChat } from './useChat';

const mockDelete = jest.fn();
jest.mock('expo-file-system', () => ({
  File: jest.fn().mockImplementation((uri: string) => ({ uri, delete: () => mockDelete(uri) })),
}));
jest.mock('./stream', () => ({
  EventStream: jest.fn().mockImplementation(() => ({ start: jest.fn(), stop: jest.fn(), kick: jest.fn() })),
}));
jest.mock('./client', () => {
  const actual = jest.requireActual('./client');
  return {
    HttpError: actual.HttpError,
    getSnapshot: jest.fn(),
    sendMessage: jest.fn(),
    sendVoice: jest.fn(),
    findRequest: jest.fn(),
    warm: jest.fn(),
    newChat: jest.fn(),
    decideApproval: jest.fn(),
  };
});

const api = client as jest.Mocked<typeof client>;
const cfg = { baseUrl: 'https://jarvis.example/mobile', token: 't' };
const snapshot: Snapshot = { schema_version: 1, epoch: 'e', seq: 0, exchanges: [], requests: [], running: null };
const heard = (id: string, text: string): RequestStatus => ({
  request_id: id, state: 'queued', input: 'voice', text, run_id: null, answer: null,
  received_at: 1, finished_at: null, steered: false,
});
const URI = 'file:///cache/rec-1.m4a';

beforeEach(() => {
  jest.clearAllMocks();
  api.getSnapshot.mockResolvedValue(snapshot);
});

async function mounted() {
  const hook = await renderHook(() => useChat(cfg));
  await waitFor(() => expect(hook.result.current.state.loaded).toBe(true));
  return hook;
}

const only = (h: Awaited<ReturnType<typeof mounted>>) => Object.values(h.result.current.state.requests);

it('buffers 200 deltas until a batch, but flushes narration before a tool and final answer', async () => {
  const h = await mounted();
  const send = streamEvents();
  await act(() => {
    for (let n = 1; n <= 200; n++) send({ type: 'response', seq: n, epoch: 'e', at: n, request_id: 'request-1', delta: `${n} ` });
  });
  expect(h.result.current.state.drafts['request-1']).toBeUndefined();
  await act(() => send({ type: 'tool_call', seq: 201, epoch: 'e', at: 201, request_id: 'request-1', tool: 'search' }));
  expect(h.result.current.state.steps['request-1'][0]).toEqual({ kind: 'say', text: Array.from({ length: 200 }, (_, n) => `${n + 1} `).join('').trim() });
  await act(() => send({ type: 'response', seq: 202, epoch: 'e', at: 202, request_id: 'request-1', delta: 'Final.' }));
  await act(() => send({ type: 'run_finished', seq: 203, epoch: 'e', at: 203, request_id: 'request-1', outcome: 'answered', answer: 'Final.' }));
  expect(h.result.current.state.seq).toBe(203);
  expect(h.result.current.state.drafts).toEqual({});
});

it('drains pending text on unmount instead of leaving a timer behind', async () => {
  const actual = batching.eventBatch;
  const drained = jest.fn();
  const spy = jest.spyOn(batching, 'eventBatch').mockImplementation((deliver) => actual((events) => {
    drained(events);
    deliver(events);
  }));
  try {
    const h = await mounted();
    await act(() => streamEvents()({ type: 'response', seq: 1, epoch: 'e', at: 1, request_id: 'request-1', delta: 'Pending.' }));
    expect(drained).not.toHaveBeenCalled();
    await h.unmount();
    expect(drained).toHaveBeenCalledTimes(1);
    expect(drained.mock.calls[0][0][0].delta).toBe('Pending.');
  } finally { spy.mockRestore(); }
});

it('sends a recording and shows what was heard; the file is then deleted', async () => {
  api.sendVoice.mockImplementation(async (_c, id) => ({ transcript: 'call Samir', status: heard(id, 'call Samir'), stt: null }));
  const h = await mounted();
  let line: string | null = 'unset';
  await act(async () => { line = await h.result.current.sendRecording(URI, 2_000); });
  expect(line).toBeNull();
  expect(only(h)).toEqual([expect.objectContaining({ text: 'call Samir', input: 'voice', state: 'queued' })]);
  expect(mockDelete).toHaveBeenCalledWith(URI);
});

it('heard nothing: no message stays, the owner is told, the file is deleted', async () => {
  api.sendVoice.mockRejectedValue(new client.HttpError(422, 'I heard nothing in that recording'));
  const h = await mounted();
  let line: string | null = null;
  await act(async () => { line = await h.result.current.sendRecording(URI, 2_000); });
  expect(line).toBe('I heard nothing in that recording');
  expect(only(h)).toEqual([]);
  expect(api.findRequest).not.toHaveBeenCalled();
  expect(mockDelete).toHaveBeenCalledWith(URI);
});

it('a lost upload keeps the file; Retry sends the same file under the same id', async () => {
  api.sendVoice.mockRejectedValueOnce(new Error('Network request failed'));
  api.findRequest.mockResolvedValueOnce(null);
  const h = await mounted();
  await act(async () => { await h.result.current.sendRecording(URI, 2_000); });
  const [failed] = only(h);
  expect(failed).toMatchObject({ state: 'send_failed', audio: URI, input: 'voice' });
  expect(mockDelete).not.toHaveBeenCalled();

  api.sendVoice.mockImplementationOnce(async (_c, id) => ({ transcript: 'hi', status: heard(id, 'hi'), stt: null }));
  await act(async () => { await h.result.current.retry(failed.request_id); });
  expect(api.sendVoice).toHaveBeenLastCalledWith(cfg, failed.request_id, URI, false, undefined);
  expect(only(h)).toEqual([expect.objectContaining({ request_id: failed.request_id, text: 'hi', state: 'queued' })]);
  expect(mockDelete).toHaveBeenCalledWith(URI);
});

it('an upload whose answer was lost but which the server got is not shown as failed', async () => {
  api.sendVoice.mockRejectedValue(new Error('timeout'));
  api.findRequest.mockImplementation(async (_c, id) => heard(id, 'what time is it'));
  const h = await mounted();
  await act(async () => { await h.result.current.sendRecording(URI, 2_000); });
  expect(only(h)).toEqual([expect.objectContaining({ text: 'what time is it', state: 'queued' })]);
  expect(mockDelete).toHaveBeenCalledWith(URI);
});

it('a tap too short to be speech is dropped without an upload', async () => {
  const h = await mounted();
  await act(async () => { await h.result.current.sendRecording(URI, MIN_VOICE_MS - 1); });
  expect(api.sendVoice).not.toHaveBeenCalled();
  expect(only(h)).toEqual([]);
  expect(mockDelete).toHaveBeenCalledWith(URI);
});

it('starting to record warms the transcriber', async () => {
  const h = await mounted();
  await act(() => h.result.current.warmUp());
  expect(api.warm).toHaveBeenCalledWith(cfg);
});

// ------------------------------------------------------------------ M3: answers read aloud

function streamEvents() {
  const { EventStream } = jest.requireMock('./stream') as { EventStream: jest.Mock };
  return EventStream.mock.calls.at(-1)[1].onEvent as (e: Record<string, unknown>) => void;
}

it('the answer to a voice message is read aloud once, from the final answer only', async () => {
  const onSpokenAnswer = jest.fn();
  api.sendVoice.mockImplementation(async (_c, id) => ({ transcript: 'call Samir', status: heard(id, 'call Samir'), stt: null }));
  const hook = await renderHook(() => useChat(cfg, { onSpokenAnswer }));
  await waitFor(() => expect(hook.result.current.state.loaded).toBe(true));
  await act(async () => { await hook.result.current.sendRecording(URI, 2_000, true); });
  const [r] = Object.values(hook.result.current.state.requests);
  expect(api.sendVoice).toHaveBeenLastCalledWith(cfg, r.request_id, URI, true, undefined);
  const send = streamEvents();
  await act(() => send({ type: 'run_started', seq: 1, epoch: 'e', at: 0, request_id: r.request_id }));
  await act(() => send({ type: 'response', seq: 2, epoch: 'e', at: 0, request_id: r.request_id, delta: 'Draft…' }));
  expect(onSpokenAnswer).not.toHaveBeenCalled();                   // a draft is never spoken
  // A snapshot mid-turn (the app came back to the foreground) does not lose the wish.
  api.getSnapshot.mockResolvedValue({ ...snapshot, seq: 2, requests: [{ ...heard(r.request_id, 'call Samir'), state: 'running' }] });
  await act(async () => { await hook.result.current.refresh(); });
  await act(() => send({ type: 'run_finished', seq: 3, epoch: 'e', at: 0, request_id: r.request_id, outcome: 'answered', answer: 'Calling Samir at 6.' }));
  expect(onSpokenAnswer).toHaveBeenCalledTimes(1);
  expect(onSpokenAnswer).toHaveBeenCalledWith(`req:${r.request_id}`, 'Calling Samir at 6.');
  api.getSnapshot.mockResolvedValue({ ...snapshot, seq: 3, requests: [{ ...heard(r.request_id, 'call Samir'), state: 'answered', answer: 'Calling Samir at 6.' }] });
  await act(async () => { await hook.result.current.refresh(); });
  expect(onSpokenAnswer).toHaveBeenCalledTimes(1);                 // once, not again per snapshot
});

async function spokenTurn(options: Parameters<typeof useChat>[1]) {
  api.sendVoice.mockImplementation(async (_c, id) => ({ transcript: 'hi', status: heard(id, 'hi'), stt: null }));
  const hook = await renderHook(() => useChat(cfg, options));
  await waitFor(() => expect(hook.result.current.state.loaded).toBe(true));
  await act(async () => { await hook.result.current.sendRecording(URI, 2_000, true); });
  await act(async () => { await hook.result.current.sendRecording(URI, 2_000, false); });
  const [wanted, quiet] = hook.result.current.state.order;
  return { hook, wanted, quiet, send: streamEvents() };
}

it('live speech: clips and the end of a spoken turn are forwarded, and its answer is not read again', async () => {
  const onSpokenAnswer = jest.fn();
  const onLiveSpeech = jest.fn();
  const { wanted, quiet, send } = await spokenTurn({ onSpokenAnswer, onLiveSpeech });
  const clip = { type: 'speech_clip', clip: 0, seq: 41, epoch: 'e', at: 0, request_id: wanted, text: 'Hello.', lang: 'en' };
  const end = { type: 'speech_end', seq: 1, epoch: 'e', at: 0, request_id: wanted, clips: 1 };
  await act(() => send(clip));
  await act(() => send(end));
  await act(() => send({ ...clip, request_id: quiet }));            // not a spoken turn: ignored
  expect(onLiveSpeech.mock.calls).toEqual([[`req:${wanted}`, clip], [`req:${wanted}`, end]]);
  await act(() => send({ type: 'run_finished', seq: 2, epoch: 'e', at: 0, request_id: wanted, outcome: 'answered', answer: 'Hello.' }));
  expect(onSpokenAnswer).not.toHaveBeenCalled();                    // the clips already said it
});

it('no speech_clip for a spoken turn: the answer is read by the plan path exactly as before', async () => {
  const onSpokenAnswer = jest.fn();
  const onLiveSpeech = jest.fn();
  const { wanted, send } = await spokenTurn({ onSpokenAnswer, onLiveSpeech });
  await act(() => send({ type: 'run_finished', seq: 1, epoch: 'e', at: 0, request_id: wanted, outcome: 'answered', answer: 'Done.' }));
  expect(onLiveSpeech).not.toHaveBeenCalled();
  expect(onSpokenAnswer).toHaveBeenCalledWith(`req:${wanted}`, 'Done.');
});

it('a voice message sent with reading off, or a failed turn, is not read aloud', async () => {
  const onSpokenAnswer = jest.fn();
  api.sendVoice.mockImplementation(async (_c, id) => ({ transcript: 'x', status: heard(id, 'x'), stt: null }));
  const hook = await renderHook(() => useChat(cfg, { onSpokenAnswer }));
  await waitFor(() => expect(hook.result.current.state.loaded).toBe(true));
  await act(async () => { await hook.result.current.sendRecording(URI, 2_000, false); });
  await act(async () => { await hook.result.current.sendRecording(URI, 2_000, true); });
  const [quiet, failing] = hook.result.current.state.order;
  const send = streamEvents();
  await act(() => send({ type: 'run_finished', seq: 1, epoch: 'e', at: 0, request_id: quiet, outcome: 'answered', answer: 'A.' }));
  await act(() => send({ type: 'run_finished', seq: 2, epoch: 'e', at: 0, request_id: failing, outcome: 'failed', answer: null }));
  expect(onSpokenAnswer).not.toHaveBeenCalled();
});

it('a decision on the phone is sent and shown at once', async () => {
  api.decideApproval.mockImplementation(async (_c, id) => ({
    text: `Denied #${id}. Nothing was done.`,
    approval: { id, state: 'denied', preview: 'p', why: 'w', context: 'live', created_at: 1, expires_at: 2,
      decided_at: 3, decided_how: 'phone', outcome: '' },
  }));
  api.getSnapshot.mockResolvedValue({ ...snapshot, approvals: [{ id: 4, state: 'pending', preview: 'p', why: 'w',
    context: 'live', created_at: 1, expires_at: 2, decided_at: null, decided_how: null, outcome: '' }] });
  const h = await mounted();
  let text = '';
  await act(async () => { text = await h.result.current.decide(4, false); });
  expect(api.decideApproval).toHaveBeenCalledWith(cfg, 4, false);
  expect(text).toBe('Denied #4. Nothing was done.');
  expect(h.result.current.state.approvals[4].state).toBe('denied');
});

// ------------------------------------------------------------------ slice 4: answering an Inbox message

it('a message answering an Inbox item carries it, and a retry answers the same one', async () => {
  api.sendMessage.mockRejectedValueOnce(new Error('Network request failed'));
  api.findRequest.mockResolvedValueOnce(null);
  const h = await mounted();
  await act(async () => { await h.result.current.send('why that figure?', 42); });
  const [failed] = only(h);
  expect(api.sendMessage).toHaveBeenLastCalledWith(cfg, failed.request_id, 'why that figure?', 'text', 42, undefined);
  api.sendMessage.mockImplementationOnce(async (_c, id) => heard(id, 'why that figure?'));
  await act(async () => { await h.result.current.retry(failed.request_id); });
  expect(api.sendMessage).toHaveBeenLastCalledWith(cfg, failed.request_id, 'why that figure?', 'text', 42, undefined);
  api.sendMessage.mockImplementation(async (_c, id, text) => heard(id, text));
  await act(async () => { await h.result.current.send('an ordinary one'); });
  expect(api.sendMessage).toHaveBeenLastCalledWith(cfg, expect.any(String), 'an ordinary one', 'text', undefined, undefined);
});

it('a voice message answering an Inbox item carries it too', async () => {
  api.sendVoice.mockImplementation(async (_c, id) => ({ transcript: 'tell me more', status: heard(id, 'tell me more'), stt: null }));
  const h = await mounted();
  await act(async () => { await h.result.current.sendRecording(URI, 2_000, false, 7); });
  const [r] = only(h);
  expect(api.sendVoice).toHaveBeenLastCalledWith(cfg, r.request_id, URI, false, 7);
});

it('a voice turn names its request, and a tap too short to send names none', async () => {
  api.sendVoice.mockImplementation(async (_c, id) => ({ transcript: 'hello', status: heard(id, 'hello'), stt: null }));
  const h = await mounted();
  let got: { requestId: string | null; line: string | null } = { requestId: 'unset', line: null };
  await act(async () => { got = await h.result.current.sendVoiceTurn(URI, 2_000, true); });
  expect(got.requestId).toBe(only(h)[0].request_id);
  await act(async () => { got = await h.result.current.sendVoiceTurn(URI, MIN_VOICE_MS - 1, true); });
  expect(got).toEqual({ requestId: null, line: null });
});

it('a message with files names their uploads, shows them at once, and a retry names the same', async () => {
  const files = [{ kind: 'image' as const, name: 'receipt.jpg' }];
  api.sendMessage.mockRejectedValueOnce(new Error('network down'));
  api.findRequest.mockResolvedValueOnce(null);
  const h = await mounted();
  await act(async () => { await h.result.current.send('', undefined, { uploads: ['up-1'], files }); });
  const [failed] = only(h);
  expect(failed).toMatchObject({ state: 'send_failed', files, text: '' });
  expect(api.sendMessage).toHaveBeenLastCalledWith(cfg, failed.request_id, '', 'text', undefined, ['up-1']);
  const line = 'Total?\n\n[Sent image: attachments/2026-10-17-1432-receipt.md]';
  api.sendMessage.mockImplementationOnce(async (_c, id) => ({ ...heard(id, line), input: 'text' }));
  await act(async () => { await h.result.current.retry(failed.request_id); });
  expect(api.sendMessage).toHaveBeenLastCalledWith(cfg, failed.request_id, '', 'text', undefined, ['up-1']);
  expect(only(h)[0].text).toBe(line);         // the server's text, which the stored exchange will match
});

it('nothing to send: no words and no files', async () => {
  const h = await mounted();
  await act(async () => { await h.result.current.send('  ', undefined, { uploads: [], files: [] }); });
  expect(api.sendMessage).not.toHaveBeenCalled();
});

// ------------------------------------------------------------------ live voice

it('adopts a turn the server already submitted, exactly like an uploaded voice turn', async () => {
  const onSpokenAnswer = jest.fn();
  const hook = await renderHook(() => useChat(cfg, { onSpokenAnswer }));
  await waitFor(() => expect(hook.result.current.state.loaded).toBe(true));
  await act(async () => hook.result.current.adoptVoiceTurn('req-live-1', heard('req-live-1', 'call Samir'), true, 9, 'file:///rec.wav'));
  const [r] = Object.values(hook.result.current.state.requests);
  expect(r).toMatchObject({ request_id: 'req-live-1', text: 'call Samir', input: 'voice', state: 'queued' });
  expect(mockDelete).toHaveBeenCalledWith('file:///rec.wav');       // submitted: the WAV is no longer needed
  expect(api.sendVoice).not.toHaveBeenCalled();
  const send = streamEvents();
  await act(() => send({ type: 'run_finished', seq: 1, epoch: 'e', at: 1, request_id: 'req-live-1', outcome: 'answered', answer: 'Done.' }));
  expect(onSpokenAnswer).toHaveBeenCalledWith('req:req-live-1', 'Done.');
});

it('a live turn that is not to be spoken is not read aloud', async () => {
  const onSpokenAnswer = jest.fn();
  const hook = await renderHook(() => useChat(cfg, { onSpokenAnswer }));
  await waitFor(() => expect(hook.result.current.state.loaded).toBe(true));
  await act(async () => hook.result.current.adoptVoiceTurn('req-live-2', heard('req-live-2', 'hi'), false));
  const send = streamEvents();
  await act(() => send({ type: 'run_finished', seq: 1, epoch: 'e', at: 1, request_id: 'req-live-2', outcome: 'answered', answer: 'Hello.' }));
  expect(onSpokenAnswer).not.toHaveBeenCalled();
});

it('the fallback upload of a live turn reuses its request id and reply_to', async () => {
  api.sendVoice.mockImplementation(async (_c, id) => ({ transcript: 'x', status: heard(id, 'x'), stt: null }));
  const h = await mounted();
  let got: { requestId: string | null; line: string | null } = { requestId: null, line: null };
  await act(async () => { got = await h.result.current.sendVoiceTurn('file:///rec.wav', 2_000, true, 5, 'req-live-3'); });
  expect(got.requestId).toBe('req-live-3');
  expect(api.sendVoice).toHaveBeenLastCalledWith(cfg, 'req-live-3', 'file:///rec.wav', true, 5);
});

it("his own words are kept when the server's text is a different message", async () => {
  api.sendMessage.mockImplementation(async (_c, id) => ({ ...heard(id, 'something else'), input: 'text' }));
  const h = await mounted();
  await act(async () => { await h.result.current.send('my words'); });
  expect(only(h)[0].text).toBe('my words');
});
