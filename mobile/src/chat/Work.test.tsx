import { fireEvent, render, screen } from '@testing-library/react-native';

import type { Step } from './reducer';
import type { PlanStep } from './types';
import { planSummary, Work, workSummary } from './Work';

const plan: PlanStep[] = [
  { id: 1, label: 'Read the ledger', tool: 'portfolio', status: 'done' },
  { id: 2, label: 'Search news', tool: 'web_browse', status: 'failed' },
  { id: 3, label: 'Write it up', tool: null, status: 'running' },
];
const steps: Step[] = [
  { kind: 'say', text: 'Checking your position first.' },
  { kind: 'tool', tool: 'portfolio', detail: 'show', outcome: 'ok' },
  { kind: 'tool', tool: 'web_browse', detail: 'NVDA news', outcome: 'failed' },
  { kind: 'tool', tool: 'web_browse', detail: 'NVDA earnings', outcome: 'ok' },
];

it('counts what is done and what failed', () => {
  expect(planSummary(plan)).toBe('Plan · 1 of 3 done · 1 failed');
  expect(planSummary(plan.slice(0, 1))).toBe('Plan · 1 of 1 done');
});

it('sums up the plan and the steps in one line', () => {
  expect(workSummary(plan, steps)).toBe('Plan · 1 of 3 done · 1 failed · 3 steps');
  expect(workSummary(undefined, steps)).toBe('3 steps · portfolio, web_browse');
  expect(workSummary(plan, undefined)).toBe('Plan · 1 of 3 done · 1 failed');
  expect(workSummary(undefined, [{ kind: 'say', text: 'a note' }])).toBe('Notes');
  expect(workSummary(undefined, [])).toBe('');
});

it('one fold holds the plan and the steps: open while working, one line once answered', async () => {
  const { rerender } = await render(<Work plan={plan} steps={steps} running />);
  expect(screen.getByText(/Search news/)).toBeTruthy();
  expect(screen.getByText(/NVDA earnings/)).toBeTruthy();
  await rerender(<Work plan={plan} steps={steps} running={false} />);
  expect(screen.queryByText(/Search news/)).toBeNull();
  expect(screen.queryByText(/NVDA earnings/)).toBeNull();
  await fireEvent.press(screen.getByText(/Plan · 1 of 3 done/));
  expect(screen.getByText(/Search news/)).toBeTruthy();
  expect(screen.getByText(/NVDA earnings/)).toBeTruthy();
});

it('a failed tool is marked, and what he said on the way is kept', async () => {
  await render(<Work steps={steps} running />);
  expect(screen.getByText(/✕ web_browse — NVDA news/)).toBeTruthy();
  expect(screen.getByText('Checking your position first.')).toBeTruthy();
});

it('shows the thinking only while working, and nothing at all when there is nothing to show', async () => {
  const { rerender, toJSON } = await render(<Work thinking={'first thought\nlast thought'} running />);
  expect(screen.getByText(/last thought/)).toBeTruthy();
  await rerender(<Work thinking="old thought" running={false} />);
  expect(toJSON()).toBeNull();
  await rerender(<Work plan={[]} steps={[]} running />);
  expect(toJSON()).toBeNull();
});

it('opens the whole thinking on a tap', async () => {
  const long = Array.from({ length: 6 }, (_, i) => `line ${i}`).join('\n');
  await render(<Work thinking={long} running />);
  expect(screen.queryByText(/line 0/)).toBeNull();
  await fireEvent.press(screen.getByLabelText('Show all thinking'));
  expect(screen.getByText(/line 0/)).toBeTruthy();
});
