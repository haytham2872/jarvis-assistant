import type { GoalCard, GoalDetail, GoalMilestone } from '../chat/types';
import { budgetFraction, hubLine, linkedEvidence, nodeState, nowLine, pollMs, sortGoals, validBudget } from './model';

it('sorts by actionable state, then newest, without changing the input', () => {
  const goals = [{ state: 'done', created_at: 9 }, { state: 'paused', created_at: 6 }, { state: 'active', created_at: 2 },
    { state: 'waiting', created_at: 5 }, { state: 'active', created_at: 8 }] as GoalCard[];
  expect(sortGoals(goals).map((g) => g.created_at)).toEqual([8, 2, 5, 6, 9]);
  expect(goals[0].state).toBe('done');
});

it('polls active work every five seconds, slower for waiting, paused and ended goals', () => {
  expect(pollMs('active')).toBe(5000);
  for (const state of ['waiting', 'paused', 'done', 'failed'] as const) expect(pollMs(state)).toBe(60000);
});

it('uses the new total budget, refusing lower totals, non-decimals and more than twenty dollars', () => {
  expect(validBudget(' 2.5 ', 1)).toBe(2.5);
  expect(validBudget('20', 1)).toBe(20);
  expect(validBudget('2,5', 1)).toBe(2.5); // French decimal pad.
  for (const input of ['1', '0', '-2', '21', '1e1', '2,,5', '1,000.50', 'Infinity', '']) expect(validBudget(input, 1)).toBeNull();
  expect(budgetFraction(3, 2)).toBe(1);
  expect(budgetFraction(-1, 2)).toBe(0);
  expect(budgetFraction(null, 2)).toBe(0);
  expect(budgetFraction(1, 0)).toBe(0);
});

it('describes current work, questions and the live hub counts', () => {
  expect(nowLine({ now: 'Reading fees', waiting: [] })).toBe('Reading fees');
  expect(nowLine({ now: null, waiting: [{ label: 'Which funds?' }] as GoalCard['waiting'] })).toBe('Waiting: Which funds?');
  expect(hubLine({ enabled: true, active: 2, questions: 1, proposals: 3 })).toBe('2 active · 1 question · 3 offered');
  expect(hubLine({ enabled: false, active: 0, questions: 0, proposals: 0 })).toBe('Off on this server');
});

it('only links evidence IDs returned for this goal', () => {
  expect(linkedEvidence('Fees [ev123abc], unknown [evother].', ['ev123abc']))
    .toBe('Fees [ev123abc](jarvis-evidence:ev123abc), unknown [evother].');
});

it('shows dependencies and waits, and stops a working node when its goal pauses', () => {
  const m: GoalMilestone = { id: 'M2', title: 'Compare', state: 'ready', depends: ['M1'], summary: '', outputs: [] };
  const goal = { state: 'active', waiting: [], milestones: [{ ...m, id: 'M1', state: 'ready' }, m] } as unknown as GoalDetail;
  expect(nodeState(m, goal)).toBe('waiting');
  goal.milestones[0].state = 'done';
  expect(nodeState(m, goal)).toBe('ready');
  m.state = 'working';
  expect(nodeState(m, goal)).toBe('working');
  goal.state = 'paused';
  expect(nodeState(m, goal)).toBe('waiting');
  m.state = 'done';
  expect(nodeState(m, goal)).toBe('done');
});
