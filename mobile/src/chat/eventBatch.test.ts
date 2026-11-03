import { eventBatch } from './eventBatch';
import { chatReducer, createTimeline, initialChat } from './reducer';
import type { HubEvent } from './types';

const event = (seq: number, over: Partial<HubEvent> = {}): HubEvent => ({
  type: 'response', epoch: 'e', seq, at: seq, request_id: 'request-1', delta: `${seq} `, ...over,
});
beforeEach(() => jest.useFakeTimers());
afterEach(() => jest.useRealTimers());

it('200 deltas render in one batch with exactly the original order and text', () => {
  const events = Array.from({ length: 200 }, (_, i) => event(i + 1, { type: i % 3 ? 'response' : 'thinking' }));
  const deliver = jest.fn();
  const batch = eventBatch(deliver);
  events.forEach(batch.push);
  expect(deliver).not.toHaveBeenCalled();
  jest.advanceTimersByTime(49);
  expect(deliver).not.toHaveBeenCalled();
  jest.advanceTimersByTime(1);
  expect(deliver).toHaveBeenCalledTimes(1);
  const batched = chatReducer(initialChat, { type: 'events', events: deliver.mock.calls[0][0] });
  const original = events.reduce((s, evt) => chatReducer(s, { type: 'event', evt }), initialChat);
  expect(batched).toEqual(original);
  expect(batched.drafts['request-1']).toBe(events.filter((e) => e.type === 'response').map((e) => e.delta).join(''));
});

it('flushes narration before a tool and drains the final delta before run_finished', () => {
  let state = initialChat;
  const deliver = jest.fn((events: HubEvent[]) => { state = chatReducer(state, { type: 'events', events }); });
  const batch = eventBatch(deliver);
  batch.push(event(1, { delta: 'Checking now.' }));
  batch.push(event(2, { type: 'tool_call', tool: 'search' }));
  expect(state.steps['request-1'][0]).toEqual({ kind: 'say', text: 'Checking now.' });
  batch.push(event(3, { delta: 'The answer.' }));
  batch.push(event(4, { type: 'run_finished', outcome: 'answered', answer: 'The answer.' }));
  expect(state.seq).toBe(4);
  expect(state.drafts).toEqual({});
  jest.runAllTimers();
  expect(deliver).toHaveBeenCalledTimes(4);
});

it('flush is immediate, idempotent, and preserves sequence deduplication after a replay', () => {
  const start = chatReducer(initialChat, { type: 'event', evt: event(2) });
  let state = start;
  const deliver = jest.fn((events) => { state = chatReducer(state, { type: 'events', events }); });
  const batch = eventBatch(deliver);
  [event(1), event(2), event(3)].forEach(batch.push);
  batch.flush();
  batch.flush();
  jest.runAllTimers();
  expect(deliver).toHaveBeenCalledTimes(1);
  expect(state.drafts['request-1']).toBe('2 3 ');
});

it('a live draft keeps completed rows and unrelated requests referentially stable', () => {
  const select = createTimeline();
  const state = { ...initialChat, exchanges: [{ id: 'old', session_id: 'session', user: 'Hello', assistant: 'Hi.', at: 1 }] };
  const withRequest = chatReducer(state, { type: 'send', request_id: 'request-1', text: 'Now?', at: 200_000 });
  const before = select(withRequest);
  const after = select(chatReducer(withRequest, { type: 'event', evt: event(1) }));
  expect(after[0]).toBe(before[0]);
  expect(after[1]).not.toBe(before[1]);
});
