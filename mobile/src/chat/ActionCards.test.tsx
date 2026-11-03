import { act, fireEvent, render, screen } from '@testing-library/react-native';

import { ApprovalTray, JobCard, jobConclusion } from './ActionCards';
import type { Approval, Job } from './types';

const approval = (over: Partial<Approval> = {}): Approval => ({
  id: 12, state: 'pending', preview: 'Send an email to Sarah: "Running late."', why: 'a background job wants to reply',
  context: 'background', created_at: 1, expires_at: 1e10, decided_at: null, decided_how: null, outcome: '', ...over,
});
const job = (over: Partial<Job> = {}): Job => ({
  id: 3, status: 'done', kind: 'agent', description: 'Summarise the lease', started: 1, finished: 2,
  summary: 'Lease ends in June.', result: 'The lease **ends in June**.', note_file: 'drafts/bg-3-lease.md',
  conclusion: 'It ends in June.', ...over,
});

it('shows what is asked and why, and decides once even when tapped twice', async () => {
  let release: (t: string) => void = () => undefined;
  const onDecide = jest.fn(() => new Promise<string>((r) => { release = r; }));
  await render(<ApprovalTray approvals={[approval()]} onDecide={onDecide} />);
  expect(screen.getByText('Needs your OK (#12)')).toBeTruthy();
  expect(screen.getByText('a background job wants to reply')).toBeTruthy();
  await fireEvent.press(screen.getByLabelText('Approve'));
  await fireEvent.press(screen.getByLabelText('Approve'));
  await fireEvent.press(screen.getByLabelText('Deny'));
  expect(onDecide).toHaveBeenCalledTimes(1);
  expect(onDecide).toHaveBeenCalledWith(12, true);
  await act(async () => { release('Approved #12. Email sent.'); });
  expect(screen.getByText('Approved #12. Email sent.')).toBeTruthy();
});

it('a request being carried out has no buttons; with nothing waiting the tray is gone', async () => {
  const { rerender } = await render(<ApprovalTray approvals={[approval({ state: 'executing' })]} onDecide={jest.fn()} />);
  expect(screen.getByText('Doing it (#12)…')).toBeTruthy();
  expect(screen.queryByLabelText('Approve')).toBeNull();
  await rerender(<ApprovalTray approvals={[]} onDecide={jest.fn()} />);
  expect(screen.queryByLabelText('Waiting for your approval')).toBeNull();
});

it('a finished job keeps the whole result in its card, folded; only the conclusion is a message', async () => {
  // The owner, 15 October: the research stays in the collapsible card, the conclusion is a message.
  const long = `${'Background on the lease terms and the notice period. '.repeat(20)}It ends in June.`;
  await render(<JobCard job={job({ result: long })} />);
  expect(screen.getByText('Background work done')).toBeTruthy();
  expect(screen.getByText('Saved as drafts/bg-3-lease.md')).toBeTruthy();
  expect(screen.getByText(/Background on the lease terms/)).toBeTruthy();
  await fireEvent.press(screen.getByText('Show all'));
  expect(screen.getByText('Show less')).toBeTruthy();
  expect(jobConclusion(job({ result: long }))).toBe('It ends in June.');
});

it('only a finished job with a conclusion says one; the result itself is never the message', () => {
  expect(jobConclusion(job({ status: 'running' }))).toBe('');
  expect(jobConclusion(job({ status: 'failed' }))).toBe('');
  expect(jobConclusion(job({ conclusion: undefined }))).toBe('');      // an older job: card only
  expect(jobConclusion(job({ conclusion: '' }))).toBe('');
});

it('a long failure reason still folds', async () => {
  await render(<JobCard job={job({ status: 'failed', result: '', summary: 'word '.repeat(300) })} />);
  await fireEvent.press(screen.getByText('Show all'));
  expect(screen.getByText('Show less')).toBeTruthy();
});

it('running and failed jobs say so', async () => {
  await render(<JobCard job={job({ status: 'running', result: '', summary: '' })} />);
  expect(screen.getByText('Working in the background')).toBeTruthy();
  await screen.unmount();
  await render(<JobCard job={job({ status: 'failed', result: '', summary: 'Timed out after 10 minutes.' })} />);
  expect(screen.getByText('Background work stopped')).toBeTruthy();
  expect(screen.getByText('Timed out after 10 minutes.')).toBeTruthy();
});
