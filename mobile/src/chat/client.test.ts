// The wire format of the voice calls: what the server sees, and how its errors come back.
import {
  answerGoal, controlGoal, decideGoalProposal, fetchClipBytes, getActivity, getGoalFile, getPortfolio, getSpeechPlan, getToday,
  HttpError, pauseWork, raiseGoalBudget, resumeWork, sendVoice, tapBlock, transcribeGoalAnswer,
  addWeekBlock, applyWeek, getWeek, moveWeekBlock, undoWeek,
  getRecord, getOvernight, getVaultNote, getHub,
} from './client';
import { empty as emptyRecord } from '../record/fixtures';

const mockUpload = jest.fn();
jest.mock('expo-file-system', () => ({
  File: jest.fn().mockImplementation((uri: string) => ({ uri, upload: (...a: unknown[]) => mockUpload(uri, ...a) })),
  UploadType: { BINARY_CONTENT: 0 },
}));

const cfg = { baseUrl: 'https://jarvis.example', token: 'tok' };
const json = (status: number, body: unknown) => ({
  ok: status < 400, status, headers: { get: () => 'application/json' }, json: async () => body,
});

beforeEach(() => jest.clearAllMocks());

it('authenticates overnight/note reads, encodes note names and keeps the same window on 304', async () => {
  const view = { as_of: 1792912800, since: 1792872000, items: [], notes: [], complete: true };
  const fetchMock = jest.fn().mockResolvedValue(json(200, view)); global.fetch = fetchMock as unknown as typeof fetch;
  await getOvernight(cfg, view.since);
  await getVaultNote(cfg, 'topics', 'rates & duration.md');
  await getHub(cfg, undefined, view.since);
  expect(fetchMock.mock.calls.map((c) => c[0])).toEqual([
    'https://jarvis.example/mobile/v1/overnight?since=1792872000',
    'https://jarvis.example/mobile/v1/vault/note?folder=topics&name=rates%20%26%20duration.md',
    'https://jarvis.example/mobile/v1/hub?since=1792872000',
  ]);
  expect(fetchMock.mock.calls.every((c) => c[1].headers.Authorization === 'Bearer tok')).toBe(true);
  fetchMock.mockResolvedValue({ status: 304, headers: { get: () => 'W/"quiet"' } });
  expect(await getOvernight(cfg, view.since, { etag: 'W/"quiet"', cached: view })).toBe(view);
});

it('keeps an empty record on 304 without fabricating a score or dropping authentication', async () => {
  const view = emptyRecord(), json = jest.fn();
  const fetchMock = jest.fn().mockResolvedValue({ status: 304, headers: { get: () => 'W/"empty-record"' }, json });
  global.fetch = fetchMock as unknown as typeof fetch;
  expect(await getRecord(cfg, { cached: view, etag: 'W/"empty-record"' })).toBe(view);
  expect(fetchMock.mock.calls[0][0]).toBe('https://jarvis.example/mobile/v1/record');
  expect(fetchMock.mock.calls[0][1].headers.Authorization).toBe('Bearer tok');
  expect(json).not.toHaveBeenCalled();
});

it('uses dedicated week routes and sends the exact slot and expected version', async () => {
  const fetchMock = jest.fn().mockResolvedValue(json(200, {})); global.fetch = fetchMock as unknown as typeof fetch;
  const context = { view_start: '2026-10-19', days: 3, plan_id: 10 };
  await getWeek(cfg, context.view_start);
  await moveWeekBlock(cfg, 'gym/1', context, '2026-10-20', '15:30');
  await addWeekBlock(cfg, context, '2026-10-20', '17:00', 'gym', 45);
  await undoWeek(cfg, context);
  await applyWeek(cfg, context, '2026-10-19', 11);
  expect(fetchMock.mock.calls.map((c) => c[0])).toEqual([
    'https://jarvis.example/mobile/v1/week?start=2026-10-19&days=3',
    'https://jarvis.example/mobile/v1/week/blocks/gym%2F1/move', 'https://jarvis.example/mobile/v1/week/blocks',
    'https://jarvis.example/mobile/v1/week/undo', 'https://jarvis.example/mobile/v1/week/apply',
  ]);
  expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({ ...context, day: '2026-10-20', start: '15:30' });
  expect(JSON.parse(fetchMock.mock.calls[2][1].body)).toEqual({ ...context, day: '2026-10-20', start: '17:00', kind: 'gym', minutes: 45 });
  expect(JSON.parse(fetchMock.mock.calls[3][1].body)).toEqual(context);
  expect(JSON.parse(fetchMock.mock.calls[4][1].body)).toEqual({ ...context, week: '2026-10-19', plan_id: 11 });
});

it('sends goal actions and exact answer text through their dedicated routes', async () => {
  const fetchMock = jest.fn().mockResolvedValue(json(200, { text: 'OK', goal: {} }));
  global.fetch = fetchMock as unknown as typeof fetch;
  await controlGoal(cfg, 'g one', 'pause');
  await raiseGoalBudget(cfg, 'g one', 2.5);
  await answerGoal(cfg, 'w1', ' CSPX, please. ');
  await decideGoalProposal(cfg, 'p1', false);
  await getGoalFile(cfg, 'g one', 'report notes.md');
  expect(fetchMock.mock.calls.map((c) => c[0])).toEqual([
    'https://jarvis.example/mobile/v1/goals/g%20one/control', 'https://jarvis.example/mobile/v1/goals/g%20one/budget',
    'https://jarvis.example/mobile/v1/goals/answer', 'https://jarvis.example/mobile/v1/goals/proposals/p1/decline',
    'https://jarvis.example/mobile/v1/goals/g%20one/files/report%20notes.md',
  ]);
  expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({ usd: 2.5 });
  expect(JSON.parse(fetchMock.mock.calls[2][1].body)).toEqual({ wait_id: 'w1', text: ' CSPX, please. ' });
});

it('uploads a goal answer for transcription without creating a chat or submitting the answer', async () => {
  mockUpload.mockResolvedValue({ status: 200, body: JSON.stringify({ transcript: 'CSPX' }) });
  const fetchMock = jest.fn(); global.fetch = fetchMock as unknown as typeof fetch;
  expect(await transcribeGoalAnswer(cfg, 'w one', 'file:///answer.m4a')).toBe('CSPX');
  expect(mockUpload.mock.calls[0][1]).toBe('https://jarvis.example/mobile/v1/goals/answer/voice?wait_id=w%20one');
  expect(mockUpload.mock.calls[0][2].headers.Authorization).toBe('Bearer tok');
  expect(fetchMock).not.toHaveBeenCalled();
  mockUpload.mockResolvedValue({ status: 409, body: JSON.stringify({ detail: 'Already settled.' }) });
  await expect(transcribeGoalAnswer(cfg, 'w1', 'file:///answer.m4a')).rejects.toMatchObject({ status: 409, message: 'Already settled.' });
});

it('sends its cached ETag and retains the same view on a bodyless 304', async () => {
  const view = { as_of: 1, paused: null, sections: [] };
  const body = jest.fn();
  const fetchMock = jest.fn().mockResolvedValue({
    status: 304, ok: false, headers: { get: (key: string) => key === 'etag' ? 'W/"a"' : null }, json: body,
  });
  global.fetch = fetchMock as unknown as typeof fetch;
  const onEtag = jest.fn();
  expect(await getActivity(cfg, { cached: view, etag: 'W/"a"', onEtag })).toBe(view);
  expect(fetchMock.mock.calls[0][1].headers['If-None-Match']).toBe('W/"a"');
  expect(body).not.toHaveBeenCalled();
  expect(onEtag).toHaveBeenCalledWith('W/"a"');
});

it('records a changed view validator, and does not send a validator without its body', async () => {
  const view = { as_of: 2, paused: null, sections: [] };
  const fetchMock = jest.fn().mockResolvedValue({
    status: 200, ok: true, headers: { get: (key: string) => key === 'etag' ? 'W/"b"' : 'application/json' }, json: async () => view,
  });
  global.fetch = fetchMock as unknown as typeof fetch;
  const onEtag = jest.fn();
  expect(await getActivity(cfg, { etag: 'W/"a"', onEtag })).toBe(view);
  expect(fetchMock.mock.calls[0][1].headers['If-None-Match']).toBeUndefined();
  expect(onEtag).toHaveBeenCalledWith('W/"b"');
});

it('a voice message asks for its answer to be read aloud only when it will be', async () => {
  mockUpload.mockResolvedValue({ status: 202, body: JSON.stringify({ transcript: 'hi', status: {}, stt: null }) });
  await sendVoice(cfg, 'req-1', 'file:///a.m4a', true);
  await sendVoice(cfg, 'req-2', 'file:///b.m4a');
  expect(mockUpload.mock.calls[0][1]).toBe('https://jarvis.example/mobile/v1/recordings?request_id=req-1&spoken=1');
  expect(mockUpload.mock.calls[1][1]).toBe('https://jarvis.example/mobile/v1/recordings?request_id=req-2');
  expect(mockUpload.mock.calls[0][2].headers.Authorization).toBe('Bearer tok');
});

it('asks for a plan and a clip by source, and reports the status of a refused clip', async () => {
  const fetchMock = jest.fn()
    .mockResolvedValueOnce(json(200, { source: 'req:a b', revision: 'r', summarised: false, clips: [] }))
    .mockResolvedValueOnce({ ok: true, status: 200, headers: { get: () => 'audio/mpeg' }, arrayBuffer: async () => new Uint8Array([1, 2]).buffer })
    .mockResolvedValueOnce(json(503, { detail: "this month's free voice allowance is used up" }));
  global.fetch = fetchMock as unknown as typeof fetch;
  await getSpeechPlan(cfg, 'req:a b');
  expect(fetchMock.mock.calls[0][0]).toBe('https://jarvis.example/mobile/v1/speech/plan?source=req%3Aa%20b');
  expect(Array.from(await fetchClipBytes(cfg, 'ex:x', 'r', 2))).toEqual([1, 2]);
  expect(fetchMock.mock.calls[1][0]).toBe('https://jarvis.example/mobile/v1/speech/clip?source=ex%3Ax&revision=r&seq=2');
  const err = await fetchClipBytes(cfg, 'ex:x', 'r', 3).catch((e) => e);
  expect(err).toBeInstanceOf(HttpError);
  expect(err.status).toBe(503);
});

it('reads the activity view and pauses or resumes the same way /pause and /resume do', async () => {
  const view = { as_of: 1, paused: null, sections: [] };
  const fetchMock = jest.fn()
    .mockResolvedValueOnce(json(200, view))
    .mockResolvedValueOnce(json(200, { text: 'Paused.', activity: view }))
    .mockResolvedValueOnce(json(200, { text: 'Paused.', activity: view }))
    .mockResolvedValueOnce(json(200, { text: 'Resumed.', activity: view }));
  global.fetch = fetchMock as unknown as typeof fetch;
  expect(await getActivity(cfg)).toEqual(view);
  expect(fetchMock.mock.calls[0][0]).toBe('https://jarvis.example/mobile/v1/activity');
  await pauseWork(cfg, 3, false);
  await pauseWork(cfg, null, true);
  expect(fetchMock.mock.calls[1][0]).toBe('https://jarvis.example/mobile/v1/pause');
  expect(fetchMock.mock.calls[1][1].method).toBe('POST');
  expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({ hours: 3, all: false });
  expect(JSON.parse(fetchMock.mock.calls[2][1].body)).toEqual({ hours: null, all: true });
  expect((await resumeWork(cfg)).text).toBe('Resumed.');
  expect(fetchMock.mock.calls[3][0]).toBe('https://jarvis.example/mobile/v1/resume');
  expect(fetchMock.mock.calls[3][1].headers.Authorization).toBe('Bearer tok');
});

it("reads today and records a block's outcome, the 409 reason kept as the error", async () => {
  const fetchMock = jest.fn()
    .mockResolvedValueOnce(json(200, { date: '2026-10-06', blocks: [] }))
    .mockResolvedValueOnce(json(200, { text: 'Gym 17:30-18:30: done.', asked: [], today: { blocks: [] } }))
    .mockResolvedValueOnce(json(409, { detail: 'Gym 17:30-18:30 has not started yet' }));
  global.fetch = fetchMock as unknown as typeof fetch;
  expect((await getToday(cfg)).date).toBe('2026-10-06');
  expect(fetchMock.mock.calls[0][0]).toBe('https://jarvis.example/mobile/v1/today');
  expect((await tapBlock(cfg, 'a/b c', 'done')).text).toBe('Gym 17:30-18:30: done.');
  expect(fetchMock.mock.calls[1][0]).toBe('https://jarvis.example/mobile/v1/today/blocks/a%2Fb%20c');
  expect(fetchMock.mock.calls[1][1].method).toBe('POST');
  expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({ outcome: 'done' });
  const err = await tapBlock(cfg, 'g', 'partly').catch((e) => e);
  expect(err).toBeInstanceOf(HttpError);
  expect([err.status, err.message]).toEqual([409, 'Gym 17:30-18:30 has not started yet']);
});

it('reads the portfolio with the token', async () => {
  const fetchMock = jest.fn().mockResolvedValueOnce(json(200, { positions: [], theses: [] }));
  global.fetch = fetchMock as unknown as typeof fetch;
  expect((await getPortfolio(cfg)).positions).toEqual([]);
  expect(fetchMock.mock.calls[0][0]).toBe('https://jarvis.example/mobile/v1/portfolio');
  expect(fetchMock.mock.calls[0][1].headers.Authorization).toBe('Bearer tok');
});
