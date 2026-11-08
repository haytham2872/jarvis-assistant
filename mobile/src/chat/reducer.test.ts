import { chatReducer, initialChat, newRequestId, timeline, waitingApprovals, type ChatState } from './reducer';
import type { HubEvent, RequestStatus, Snapshot } from './types';

const status = (id: string, state: RequestStatus['state'], extra: Partial<RequestStatus> = {}): RequestStatus => ({
  request_id: id, state, input: 'text', text: `text of ${id}`, run_id: null, answer: null,
  received_at: 1_000, finished_at: null, steered: false, ...extra,
});
const snap = (over: Partial<Snapshot> = {}): Snapshot => ({
  schema_version: 1, epoch: 'e1', seq: 10, exchanges: [], requests: [], running: null, ...over,
});
let seq = 10;
const evt = (type: string, fields: Partial<HubEvent> = {}): HubEvent => ({ type, seq: ++seq, epoch: 'e1', at: 0, ...fields });
const apply = (s: ChatState, ...actions: Parameters<typeof chatReducer>[1][]) => actions.reduce(chatReducer, s);

beforeEach(() => { seq = 10; });

describe('sending', () => {
  it('shows a message at once, then follows it through the turn to the answer', () => {
    let s = apply(initialChat, { type: 'snapshot', snap: snap() },
      { type: 'send', request_id: 'r1', text: 'what is NVDA at?', at: 5 });
    expect(s.requests.r1.state).toBe('sending');
    s = apply(s, { type: 'sent', status: status('r1', 'queued') },
      { type: 'event', evt: evt('run_started', { request_id: 'r1' }) },
      { type: 'event', evt: evt('response', { request_id: 'r1', delta: 'It is ' }) },
      { type: 'event', evt: evt('response', { request_id: 'r1', delta: '$181.' }) });
    expect(s.requests.r1.state).toBe('running');
    expect(s.drafts.r1).toBe('It is $181.');
    s = apply(s, { type: 'event', evt: evt('run_finished', { request_id: 'r1', outcome: 'answered', answer: 'It is $181.40.' }) });
    expect(s.requests.r1).toMatchObject({ state: 'answered', answer: 'It is $181.40.', text: 'what is NVDA at?' });
    expect(s.drafts.r1).toBeUndefined();               // the final answer replaces the draft
  });

  it('keeps a failed send visible with its error, for a retry with the same id', () => {
    const s = apply(initialChat, { type: 'send', request_id: 'r1', text: 'hi', at: 1 },
      { type: 'send_failed', request_id: 'r1', error: 'offline' });
    expect(s.requests.r1).toMatchObject({ state: 'send_failed', error: 'offline', text: 'hi' });
  });

  it('reports a failed or cut-off turn instead of leaving it spinning', () => {
    const s = apply(initialChat, { type: 'snapshot', snap: snap({ requests: [status('r1', 'running')] }) },
      { type: 'event', evt: evt('run_finished', { request_id: 'r1', outcome: 'interrupted', answer: null }) });
    expect(s.requests.r1.state).toBe('interrupted');
  });
});

describe('events', () => {
  it('ignores an event it has already seen (a replay after reconnect)', () => {
    let s = apply(initialChat, { type: 'snapshot', snap: snap({ requests: [status('r1', 'running')] }) });
    const e = evt('response', { request_id: 'r1', delta: 'once' });
    s = apply(s, { type: 'event', evt: e }, { type: 'event', evt: e });
    expect(s.drafts.r1).toBe('once');
  });

  it('accepts events from a new server process', () => {
    let s = apply(initialChat, { type: 'snapshot', snap: snap({ requests: [status('r1', 'running')] }) });
    s = apply(s, { type: 'event', evt: { type: 'response', seq: 1, epoch: 'e2', at: 0, request_id: 'r1', delta: 'x' } });
    expect(s.drafts.r1).toBe('x');
    expect(s.epoch).toBe('e2');
  });

  it('keeps only a bounded glimpse of the thinking', () => {
    let s = apply(initialChat, { type: 'snapshot', snap: snap({ requests: [status('r1', 'running')] }) });
    for (let i = 0; i < 50; i++) s = apply(s, { type: 'event', evt: evt('thinking', { request_id: 'r1', delta: 'x'.repeat(100) }) });
    expect(s.activity.r1.length).toBe(2_000);
  });

  it('a new chat clears the conversation', () => {
    const s = apply(initialChat, { type: 'snapshot', snap: snap({ requests: [status('r1', 'answered')] }) },
      { type: 'event', evt: evt('chat_started') });
    expect(s.order).toEqual([]);
    expect(timeline(s)).toEqual([]);
  });
});

describe('snapshot', () => {
  it('is the truth for what the server knows, but keeps sends it never received', () => {
    let s = apply(initialChat, { type: 'send', request_id: 'mine', text: 'still sending', at: 1 },
      { type: 'send', request_id: 'gone', text: 'stale', at: 1 }, { type: 'sent', status: status('gone', 'queued') });
    s = apply(s, { type: 'snapshot', snap: snap({ requests: [status('srv', 'answered', { answer: 'A' })] }) });
    expect(Object.keys(s.requests).sort()).toEqual(['mine', 'srv']);
    expect(s.requests.srv).toMatchObject({ text: 'text of srv', answer: 'A' });
  });
});

describe('timeline', () => {
  it('shows history, then requests the history does not hold yet', () => {
    const s = apply(initialChat, { type: 'snapshot', snap: snap({
      exchanges: [{ id: 'x1', session_id: 's', at: 2, user: 'text of done', assistant: 'done answer' }],
      requests: [status('done', 'answered', { received_at: 1.5, answer: 'done answer' }), status('live', 'running', { received_at: 3 })],
    }) });
    const items = timeline(s);
    expect(items.map((i) => i.key)).toEqual(['x-x1', 'r-live']);   // 'done' is already in history
  });

  it('does not show a message that joined another turn as its own', () => {
    const s = apply(initialChat, { type: 'snapshot', snap: snap({ requests: [status('r2', 'absorbed', { steered: true })] }) });
    expect(timeline(s)).toEqual([]);
  });
});

it('makes request ids the server accepts', () => {
  const id = newRequestId(1_760_000_000_000, () => 0.5);
  expect(id).toMatch(/^[A-Za-z0-9_-]{8,64}$/);
  expect(newRequestId()).not.toBe(newRequestId());
});

describe('steps (the owner, 14 October: the tool lines vanished when the answer arrived)', () => {
  const live = () => apply(initialChat, { type: 'snapshot', snap: snap({ requests: [status('r1', 'running', { text: 'NVDA close?' })] }) },
    { type: 'event', evt: evt('run_started', { request_id: 'r1' }) },
    { type: 'event', evt: evt('response', { request_id: 'r1', delta: 'Let me check the close.' }) },
    { type: 'event', evt: evt('tool_call', { request_id: 'r1', tool: 'web_browse', detail: 'NVDA close today' }) },
    { type: 'event', evt: evt('response', { request_id: 'r1', delta: 'It closed at $181.' }) },
    { type: 'event', evt: evt('run_finished', { request_id: 'r1', outcome: 'answered', answer: 'It closed at $181.40.' }) });

  it('keeps what he said before a tool, and the tool, after the final answer replaces the draft', () => {
    const s = live();
    expect(s.requests.r1.answer).toBe('It closed at $181.40.');
    expect(s.steps.r1).toEqual([
      { kind: 'say', text: 'Let me check the close.' },
      { kind: 'tool', tool: 'web_browse', detail: 'NVDA close today' },
    ]);
  });

  it('the steps stay with the answer once it moves into the history, with the receipts outcome', () => {
    const s = apply(live(), { type: 'snapshot', snap: snap({
      seq: 99,
      exchanges: [{ id: 'x1', session_id: 's', at: 2, user: 'NVDA close?', assistant: 'It closed at $181.40.' }],
      requests: [status('r1', 'answered', { text: 'NVDA close?', received_at: 1, answer: 'It closed at $181.40.',
        tools: [{ tool: 'web_browse', outcome: 'ok', detail: 'NVDA close today' }] })],
    }) });
    const items = timeline(s);
    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({ kind: 'exchange', key: 'x-x1' });
    expect(items[0].kind === 'exchange' && items[0].steps).toEqual([
      { kind: 'say', text: 'Let me check the close.' },
      { kind: 'tool', tool: 'web_browse', detail: 'NVDA close today', outcome: 'ok' },
    ]);
  });

  it('a reopened app gets the tools back from the receipts', () => {
    const s = apply(initialChat, { type: 'snapshot', snap: snap({
      requests: [status('r9', 'answered', { answer: 'Done.', tools: [
        { tool: 'portfolio', outcome: 'ok', detail: 'show' }, { tool: 'web_browse', outcome: 'failed', detail: 'TTWO' }] })],
    }) });
    expect(s.steps.r9.map((x) => x.kind === 'tool' && `${x.tool}:${x.outcome}`)).toEqual(['portfolio:ok', 'web_browse:failed']);
  });
});

describe('voice messages', () => {
  const voice = (id: string, state: RequestStatus['state'], text: string) => status(id, state, { input: 'voice', text });

  it('shows a placeholder while transcribing, then the heard text', () => {
    let s = apply(initialChat, { type: 'snapshot', snap: snap() },
      { type: 'send_voice', request_id: 'v1', at: 5, audio: 'file:///rec.m4a' });
    expect(s.requests.v1).toMatchObject({ state: 'transcribing', text: '', input: 'voice', audio: 'file:///rec.m4a' });
    s = apply(s, { type: 'sent', status: voice('v1', 'queued', 'Remind me to call Samir at 6') });
    expect(s.requests.v1).toMatchObject({ state: 'queued', text: 'Remind me to call Samir at 6', input: 'voice' });
    expect(s.requests.v1.audio).toBeUndefined();        // the server has it: no copy to resend
  });

  it('removes a recording in which nothing was heard: no empty message', () => {
    const s = apply(initialChat, { type: 'send_voice', request_id: 'v1', at: 5, audio: 'file:///rec.m4a' },
      { type: 'heard_nothing', request_id: 'v1' });
    expect(s.requests.v1).toBeUndefined();
    expect(s.order).toEqual([]);
    expect(timeline(s)).toEqual([]);
  });

  it('keeps the recording of a failed upload, for a retry of the same file', () => {
    const s = apply(initialChat, { type: 'send_voice', request_id: 'v1', at: 5, audio: 'file:///rec.m4a' },
      { type: 'send_failed', request_id: 'v1', error: 'offline' });
    expect(s.requests.v1).toMatchObject({ state: 'send_failed', audio: 'file:///rec.m4a', input: 'voice' });
  });

  it('keeps a recording still transcribing across a snapshot that does not know it yet', () => {
    const s = apply(initialChat, { type: 'send_voice', request_id: 'v1', at: 5, audio: 'file:///rec.m4a' },
      { type: 'snapshot', snap: snap() });
    expect(s.requests.v1.state).toBe('transcribing');
  });

  it('moves on when the server accepts it before the upload answer arrives', () => {
    const s = apply(initialChat, { type: 'snapshot', snap: snap() },
      { type: 'send_voice', request_id: 'v1', at: 5, audio: 'file:///rec.m4a' },
      { type: 'event', evt: evt('accepted', { request_id: 'v1' }) });
    expect(s.requests.v1.state).toBe('queued');
  });

  it('marks a voice message in the history so it can be corrected', () => {
    let s = apply(initialChat, { type: 'snapshot', snap: snap() },
      { type: 'send_voice', request_id: 'v1', at: 1_000, audio: 'file:///rec.m4a' });
    s = apply(s, { type: 'sent', status: voice('v1', 'answered', 'hello there') },
      { type: 'snapshot', snap: snap({ seq: 20, requests: [voice('v1', 'answered', 'hello there')],
        exchanges: [{ id: 'x1', session_id: 's', at: 1_001, user: 'hello there', assistant: 'Hi.' }] }) });
    const items = timeline(s);
    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({ kind: 'exchange', voice: true });
  });
});

describe('approvals and background work (M4)', () => {
  const approval = (id: number, state: string, created = 1) => ({
    id, state, preview: `Send email #${id}`, why: 'w', context: 'background', created_at: created,
    expires_at: 1e10, decided_at: null, decided_how: null, outcome: '',
  }) as never;
  const job = (id: number, status: string, started: number, extra = {}) => ({
    id, status, kind: 'agent', description: `task ${id}`, started, finished: null, summary: '', result: '',
    note_file: '', ...extra,
  }) as never;

  it('the snapshot sets what waits on him; events and his decisions update it', () => {
    let s = apply(initialChat, { type: 'snapshot', snap: snap({ approvals: [approval(1, 'pending', 5), approval(2, 'pending', 3)] }) });
    expect(waitingApprovals(s).map((a) => a.id)).toEqual([2, 1]);                // oldest first, not by id
    s = apply(s, { type: 'event', evt: evt('approval', { approval: approval(2, 'denied', 3) }) });
    expect(waitingApprovals(s).map((a) => a.id)).toEqual([1]);
    s = apply(s, { type: 'approval', approval: approval(1, 'executing', 5) });
    expect(waitingApprovals(s).map((a) => [a.id, a.state])).toEqual([[1, 'executing']]);  // shown while it runs
    s = apply(s, { type: 'snapshot', snap: snap({ seq: 30, approvals: [] }) });
    expect(waitingApprovals(s)).toEqual([]);                                      // decided elsewhere
  });

  it('a job shows in the conversation where it started, and not in a new chat', () => {
    let s = apply(initialChat, { type: 'snapshot', snap: snap({
      exchanges: [{ id: 'x1', session_id: 's', at: 100, user: 'research it', assistant: 'On it.' }],
      jobs: [job(7, 'running', 101), job(6, 'done', 50)],          // 6 began before this conversation
    }) });
    expect(timeline(s).map((i) => i.key)).toEqual(['x-x1', 'j-7']);
    s = apply(s, { type: 'event', evt: evt('job', { job: job(7, 'done', 101, { result: 'Found it.' }) }) });
    const card = timeline(s).find((i) => i.kind === 'job');
    expect(card?.kind === 'job' && card.job.result).toBe('Found it.');
    s = apply(s, { type: 'event', evt: evt('chat_started') });
    expect(timeline(s)).toEqual([]);
  });

  it('a job started during a request sits after it', () => {
    const s = apply(initialChat, { type: 'snapshot', snap: snap({ jobs: [job(3, 'running', 200)] }) },
      { type: 'send', request_id: 'r1', text: 'do it in the background', at: 199_000 });
    expect(timeline(s).map((i) => i.key)).toEqual(['r-r1', 'j-3']);
  });

  it('cards and messages keep their time order: a job between an answer and a later message', () => {
    const s = apply(initialChat, { type: 'snapshot', snap: snap({
      exchanges: [{ id: 'x1', session_id: 's', at: 100, user: 'start it', assistant: 'Started.' }],
      jobs: [job(4, 'done', 150)],
    }) }, { type: 'send', request_id: 'r2', text: 'and another thing', at: 200_000 });
    expect(timeline(s).map((i) => i.key)).toEqual(['x-x1', 'j-4', 'r-r2']);
  });
});

describe('plan (M4 activity panel)', () => {
  const steps = [{ id: 1, label: 'Read the ledger', tool: 'portfolio' }, { id: 2, label: 'Search news', tool: 'web_browse' },
    { id: 3, label: 'Write it up', tool: null }];
  const running = () => apply(initialChat, { type: 'snapshot', snap: snap({ requests: [status('r1', 'running')] }) });

  it('follows each step as the server reports it, and keeps the plan with the answer', () => {
    let s = apply(running(), { type: 'event', evt: evt('plan', { request_id: 'r1', steps: steps as never }) });
    expect(s.plans.r1.map((p) => p.status)).toEqual(['pending', 'pending', 'pending']);
    s = apply(s, { type: 'event', evt: evt('step_start', { request_id: 'r1', n: 1 }) },
      { type: 'event', evt: evt('step_done', { request_id: 'r1', n: 1, success: true }) },
      { type: 'event', evt: evt('step_start', { request_id: 'r1', n: 2 }) },
      { type: 'event', evt: evt('step_done', { request_id: 'r1', n: 2, success: false }) });
    expect(s.plans.r1.map((p) => p.status)).toEqual(['done', 'failed', 'pending']);
    s = apply(s, { type: 'event', evt: evt('run_finished', { request_id: 'r1', outcome: 'answered', answer: 'ok' }) });
    const row = timeline(s).find((r) => r.kind === 'request');
    expect(row && 'plan' in row ? row.plan?.length : 0).toBe(3);
  });

  it('a replan replaces the steps; a step event for an unknown plan or step changes nothing', () => {
    let s = apply(running(), { type: 'event', evt: evt('step_start', { request_id: 'r1', n: 1 }) });
    expect(s.plans.r1).toBeUndefined();
    s = apply(s, { type: 'event', evt: evt('plan', { request_id: 'r1', steps: steps as never }) },
      { type: 'event', evt: evt('plan_update', { request_id: 'r1', steps: [{ id: 1, label: 'Save to the vault', status: 'done' }] as never }) });
    expect(s.plans.r1).toEqual([{ id: 1, label: 'Save to the vault', tool: null, status: 'done' }]);
    const before = s.plans.r1;
    s = apply(s, { type: 'event', evt: evt('step_start', { request_id: 'r1', n: 7 }) });
    expect(s.plans.r1).toEqual(before);
  });

  it('the plan stays with the answer once it moves into the history', () => {
    let s = apply(running(), { type: 'event', evt: evt('plan', { request_id: 'r1', steps: steps as never }) },
      { type: 'event', evt: evt('run_finished', { request_id: 'r1', outcome: 'answered', answer: 'ok' }) });
    s = apply(s, { type: 'snapshot', snap: snap({
      seq: 99,
      exchanges: [{ id: 'x1', session_id: 's', at: 2, user: 'text of r1', assistant: 'ok' }],
      requests: [status('r1', 'answered', { received_at: 1, answer: 'ok' })],
    }) });
    const items = timeline(s);
    expect(items).toHaveLength(1);
    expect(items[0].kind === 'exchange' && items[0].plan?.map((p) => p.label)).toEqual(
      ['Read the ledger', 'Search news', 'Write it up']);
  });

  it('a new chat clears the plans', () => {
    const s = apply(running(), { type: 'event', evt: evt('plan', { request_id: 'r1', steps: steps as never }) },
      { type: 'event', evt: evt('chat_started') });
    expect(s.plans).toEqual({});
  });
});

describe('mood (slice 6)', () => {
  it('comes from the snapshot and is replaced by each finished turn', () => {
    let s = apply(initialChat, { type: 'snapshot', snap: snap({ mood: { tone: 'curious', intensity: 0.4 } }) });
    expect(s.mood).toEqual({ tone: 'curious', intensity: 0.4 });
    s = apply(s, { type: 'event', evt: evt('run_finished', { request_id: 'r9', outcome: 'answered', answer: 'ok',
      mood: { tone: 'restless', intensity: 0.7 } }) });
    expect(s.mood).toEqual({ tone: 'restless', intensity: 0.7 });
  });

  it('a snapshot or event from a server without moods leaves the last one, and null clears it', () => {
    let s = apply(initialChat, { type: 'snapshot', snap: snap({ mood: { tone: 'engaged', intensity: 0.5 } }) });
    s = apply(s, { type: 'snapshot', snap: snap({ seq: 20 }) },
      { type: 'event', evt: evt('run_finished', { seq: 30, outcome: 'answered' }) });
    expect(s.mood).toEqual({ tone: 'engaged', intensity: 0.5 });
    s = apply(s, { type: 'event', evt: evt('run_finished', { seq: 31, outcome: 'answered', mood: null }) });
    expect(s.mood).toBeNull();
  });
});

describe('files (slice 7)', () => {
  const files = [{ kind: 'image' as const, name: 'receipt.jpg' }];
  const line = 'total?\n\n[Sent image: attachments/2026-10-17-1432-receipt.md]';

  it("takes the server's text when it adds the file line, keeping the chips and any problems", () => {
    const s = apply(initialChat, { type: 'send', request_id: 'r1', text: 'total?', at: 1, files },
      { type: 'sent', status: { ...status('r1', 'queued', { text: line, input: 'text' }), problems: ['b.png (broken)'] } });
    expect(s.requests.r1).toMatchObject({ text: line, files, problems: ['b.png (broken)'] });
  });

  it('a later status without problems keeps the earlier ones', () => {
    let s = apply(initialChat, { type: 'send', request_id: 'r2', text: 'x', at: 1, files },
      { type: 'sent', status: { ...status('r2', 'queued', { text: 'x', input: 'text' }), problems: ['p (q)'] } });
    s = apply(s, { type: 'snapshot', snap: snap({ requests: [status('r2', 'running', { text: 'x', input: 'text' })] }) });
    expect(s.requests.r2).toMatchObject({ files, problems: ['p (q)'] });
  });
});

describe('command replies', () => {
  it('shows a command and its reply as an answered message, kept across snapshots', () => {
    let s = chatReducer(initialChat, { type: 'command', request_id: 'cmd-1', text: '/pace low', reply: 'Pace set to low', at: 5 });
    expect(s.requests['cmd-1']).toMatchObject({ text: '/pace low', answer: 'Pace set to low', state: 'answered' });
    s = chatReducer(s, { type: 'snapshot', snap: snap({}) });
    expect(s.requests['cmd-1']?.answer).toBe('Pace set to low');
  });
});
