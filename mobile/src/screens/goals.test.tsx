import { act, fireEvent, render, screen, waitFor } from '@testing-library/react-native';
import { AppState, type AppStateStatus } from 'react-native';
import Goals from '../app/goals/index';
import Goal from '../app/goals/[id]';
import type { GoalCard, GoalDetail, GoalsView } from '../chat/types';
import { clearViewMemory } from '../core/useCachedView';
import { Question } from '../goals/Question';
import { MilestonePath } from '../goals/MilestonePath';
import { useState } from 'react';

const CFG = { baseUrl: 'https://j', token: 't' };
const mockApp = { cfg: CFG, speaker: { stop: jest.fn() }, setRecording: jest.fn() };
const mockPush = jest.fn();
let mockReduced = true, mockFocused = true;
const mockRepeat = jest.fn((v: number) => v), mockCancel = jest.fn();
jest.mock('../state/AppContext', () => ({ useApp: () => mockApp }));
jest.mock('expo-router', () => ({
  useFocusEffect: (fn: () => void | (() => void)) => jest.requireActual('react').useEffect(fn, [fn]),
  useLocalSearchParams: () => ({ id: 'g1' }),
  useIsFocused: () => mockFocused,
  router: { push: (...args: unknown[]) => mockPush(...args), canGoBack: () => false, replace: jest.fn() },
}));
jest.mock('react-native-safe-area-context', () => ({ useSafeAreaInsets: () => ({ top: 59, bottom: 34, left: 0, right: 0 }) }));
jest.mock('react-native-reanimated', () => ({
  __esModule: true,
  default: { View: require('react-native').View },
  useSharedValue: (v: number) => require('react').useRef({ value: v }).current,
  useAnimatedStyle: (fn: () => unknown) => fn(), withTiming: (v: number) => v,
  withRepeat: (v: number) => mockRepeat(v), cancelAnimation: (...a: unknown[]) => mockCancel(...a),
}));
jest.mock('../chat/Orb', () => ({ useReduceMotion: () => mockReduced }));
jest.mock('expo-haptics', () => ({ selectionAsync: jest.fn(), notificationAsync: jest.fn(), NotificationFeedbackType: { Success: 'success' } }));
const mockGet = jest.fn(), mockDetail = jest.fn(), mockDecide = jest.fn(), mockControl = jest.fn(), mockBudget = jest.fn();
const mockAnswer = jest.fn(), mockTranscribe = jest.fn(), mockEvidence = jest.fn(), mockFile = jest.fn();
jest.mock('../chat/client', () => {
  class HttpError extends Error { status: number; constructor(status: number, message: string) { super(message); this.status = status; } }
  return { HttpError, getGoals: (...a: unknown[]) => mockGet(...a), getGoal: (...a: unknown[]) => mockDetail(...a),
    decideGoalProposal: (...a: unknown[]) => mockDecide(...a), controlGoal: (...a: unknown[]) => mockControl(...a),
    raiseGoalBudget: (...a: unknown[]) => mockBudget(...a), answerGoal: (...a: unknown[]) => mockAnswer(...a),
    transcribeGoalAnswer: (...a: unknown[]) => mockTranscribe(...a), getGoalEvidence: (...a: unknown[]) => mockEvidence(...a),
    getGoalFile: (...a: unknown[]) => mockFile(...a) };
});
const mockRec = { start: jest.fn(), finish: jest.fn(), cancel: jest.fn() };
jest.mock('../voice/useRecorder', () => ({ useRecorder: () => mockRec }));
const card = (over: Partial<GoalCard> = {}): GoalCard => ({ goal_id: 'g1', title: 'Compare funds', state: 'active',
  created_at: 1, deadline_at: null, budget_usd: 1, spent_usd: 0.25, milestones: { done: 1, total: 2 }, now: 'Reading fees',
  waiting: [], outcome: null, ...over });
const detail = (over: Partial<GoalDetail> = {}): GoalDetail => ({ ...card(), enabled: true, reason: '', notes: [],
  milestones: [{ id: 'M1', title: 'Read fees', state: 'done', summary: 'Read the factsheets.', outputs: [], depends: [] },
    { id: 'M2', title: 'Write comparison', state: 'working', summary: '', outputs: [], depends: ['M1'] }],
  session: { state: 'running', started_at: 1, steps: ['web_browse · ok'] }, questions: [], charges: { spent_usd: 0.2, reserved_usd: 0.05 },
  files: [{ name: 'result.md', revision: 1, size: 40 }], result: 'The fee is 0.07% [ev123abc].', result_truncated: false,
  evidence: [{ id: 'ev123abc', title: 'Factsheet', url: 'https://example.com/facts', tool: 'web_browse', excerpt: 'Fee 0.07%', truncated: false }], ...over });
const listing = (over: Partial<GoalsView> = {}): GoalsView => ({ enabled: true, reason: '', goals: [card()], notes: [],
  proposals: [{ goal_id: 'p1', title: 'Research a fund', criteria: ['Compare fees'], budget_usd: 1, deadline: null, created_at: 1 }], ...over });
let onState: (s: AppStateStatus) => void;
beforeEach(() => {
  clearViewMemory(); jest.clearAllMocks();
  mockReduced = true; mockFocused = true;
  Object.defineProperty(AppState, 'currentState', { configurable: true, value: 'active' });
  jest.spyOn(AppState, 'addEventListener').mockImplementation((_event, fn) => { onState = fn; return { remove: jest.fn() }; });
  mockGet.mockResolvedValue(listing()); mockDetail.mockResolvedValue(detail());
  mockDecide.mockResolvedValue({ text: 'Started.', goal: detail() });
  mockControl.mockResolvedValue({ text: 'Paused.', goal: detail({ state: 'paused' }) });
  mockBudget.mockResolvedValue({ text: 'Raised.', goal: detail({ budget_usd: 2 }) });
  mockAnswer.mockResolvedValue({ text: 'Recorded.', goal: detail() });
  mockRec.start.mockResolvedValue(null); mockRec.finish.mockResolvedValue({ uri: 'file:///answer.m4a', ms: 1000 });
  mockRec.cancel.mockResolvedValue(undefined); mockTranscribe.mockResolvedValue('CSPX and VUAA');
  mockEvidence.mockResolvedValue({ ...detail().evidence[0], excerpt: 'Full source excerpt' });
  mockFile.mockResolvedValue({ name: 'result.md', revision: 1, size: 40, text: 'File contents', truncated: true });
});
afterEach(() => { jest.restoreAllMocks(); jest.useRealTimers(); });

it('opens a goal, and starts an offered goal without sending a chat message', async () => {
  await render(<Goals />);
  await fireEvent.press(await screen.findByLabelText('Open goal Compare funds'));
  expect(mockPush).toHaveBeenCalledWith('/goals/g1');
  expect(screen.getByText('1 of 2 milestones')).toBeTruthy();
  await act(async () => { await fireEvent.press(screen.getByLabelText('Start Research a fund')); });
  expect(mockDecide).toHaveBeenCalledWith(CFG, 'p1', true);
  expect(mockGet).toHaveBeenCalledTimes(2);
});

it('declines an offer, and hides goals and offers when the server disables them', async () => {
  await render(<Goals />);
  const decline = await screen.findByLabelText('Not now: Research a fund');
  await act(async () => { await fireEvent.press(decline); });
  expect(mockDecide).toHaveBeenCalledWith(CFG, 'p1', false);
  await screen.unmount(); clearViewMemory();
  mockGet.mockResolvedValue(listing({ enabled: false, reason: 'Goals are off on this server.' }));
  await render(<Goals />);
  expect(await screen.findByText('Goals are off on this server.')).toBeTruthy();
  expect(screen.queryByLabelText('Open goal Compare funds')).toBeNull();
  expect(screen.queryByLabelText('Start Research a fund')).toBeNull();
});

it('explains when an older server does not yet have Goals', async () => {
  const { HttpError } = jest.requireMock('../chat/client');
  mockGet.mockRejectedValue(new HttpError(404, 'Not Found'));
  await render(<Goals />);
  expect(await screen.findByText('This server does not have the Goals view yet. Update it and restart JARVIS.')).toBeTruthy();
});

it('requires confirmation before cancelling, and Keep goal does nothing', async () => {
  await render(<Goal />); await screen.findByText('Compare funds');
  await fireEvent.press(screen.getByLabelText('Cancel'));
  expect(mockControl).not.toHaveBeenCalled();
  await fireEvent.press(screen.getByLabelText('Keep goal'));
  expect(screen.queryByText('Cancel this goal?')).toBeNull();
  await fireEvent.press(screen.getByLabelText('Cancel'));
  await act(async () => { await fireEvent.press(screen.getByLabelText('Cancel goal')); });
  expect(mockControl).toHaveBeenCalledWith(CFG, 'g1', 'cancel');
});

it('changes the detail refresh interval when a goal pauses', async () => {
  const timers = jest.spyOn(global, 'setInterval');
  await render(<Goal />); await screen.findByText('Compare funds');
  expect(timers).toHaveBeenCalledWith(expect.any(Function), 5000);
  mockDetail.mockResolvedValue(detail({ state: 'paused' }));
  await act(async () => { await fireEvent.press(screen.getByLabelText('Pause')); });
  expect(timers.mock.calls.at(-1)?.[1]).toBe(60000);
});

it('pauses, resumes and raises a total cap only when it is valid', async () => {
  await render(<Goal />); await screen.findByText('Compare funds');
  mockDetail.mockResolvedValue(detail({ state: 'paused' }));
  await act(async () => { await fireEvent.press(screen.getByLabelText('Pause')); });
  expect(mockControl).toHaveBeenCalledWith(CFG, 'g1', 'pause');
  expect(screen.getByLabelText('Resume')).toBeTruthy();
  await act(async () => { await fireEvent.press(screen.getByLabelText('Resume')); });
  expect(mockControl).toHaveBeenLastCalledWith(CFG, 'g1', 'resume');
  await fireEvent.changeText(screen.getByLabelText('New total budget in dollars'), '21');
  await fireEvent.press(screen.getByLabelText('Raise budget')); expect(mockBudget).not.toHaveBeenCalled();
  await fireEvent.changeText(screen.getByLabelText('New total budget in dollars'), '2');
  await act(async () => { await fireEvent.press(screen.getByLabelText('Raise budget')); });
  expect(mockBudget).toHaveBeenCalledWith(CFG, 'g1', 2);
});

it('answers the pending question once and shows the returned state', async () => {
  mockDetail.mockResolvedValue(detail({ questions: [{ wait_id: 'w1', text: 'Which funds?' }] }));
  await render(<Goal />);
  await fireEvent.changeText(await screen.findByLabelText('Answer: Which funds?'), 'CSPX and VUAA');
  await act(async () => { await fireEvent.press(screen.getByLabelText('Send answer')); });
  expect(mockAnswer).toHaveBeenCalledWith(CFG, 'w1', 'CSPX and VUAA');
  expect(mockAnswer).toHaveBeenCalledTimes(1);
  expect(screen.queryByLabelText('Answer: Which funds?')).toBeNull();
});

it('opens a cited evidence sheet and a bounded file', async () => {
  await render(<Goal />); await screen.findByText('Compare funds');
  await act(async () => { await fireEvent.press(screen.getByText('ev123abc')); });
  expect(mockEvidence).toHaveBeenCalledWith(CFG, 'g1', 'ev123abc');
  expect(screen.getByText('Full source excerpt')).toBeTruthy();
  await fireEvent.press(screen.getByLabelText('Close'));
  await act(async () => { await fireEvent.press(screen.getByLabelText('Open result.md')); });
  expect(mockFile).toHaveBeenCalledWith(CFG, 'g1', 'result.md');
  expect(screen.getByText('File contents')).toBeTruthy();
  expect(screen.getByText('Showing the first 100 KB.')).toBeTruthy();
});

it('does not reopen a closed evidence sheet when its pending read finishes', async () => {
  let arrived!: (value: unknown) => void;
  mockEvidence.mockReturnValue(new Promise((resolve) => { arrived = resolve; }));
  await render(<Goal />); await screen.findByText('Compare funds');
  await fireEvent.press(screen.getByText('ev123abc'));
  await fireEvent.press(screen.getByLabelText('Close'));
  await act(async () => { arrived({ ...detail().evidence[0], excerpt: 'Late source excerpt' }); });
  expect(screen.queryByText('Late source excerpt')).toBeNull();
  expect(screen.queryByLabelText('Close')).toBeNull();
});

function AnswerField() {
  const [recording, setRecording] = useState<string | null>(null);
  return <Question question={{ wait_id: 'w1', text: 'Which funds?' }} cfg={CFG} disabled={false}
    recording={recording} onRecording={setRecording} onAnswer={mockAnswer} />;
}

it('records into an editable review field and only answers after Send', async () => {
  await render(<AnswerField />);
  await act(async () => { await fireEvent.press(screen.getByLabelText('Record answer')); });
  expect(mockApp.speaker.stop).toHaveBeenCalled();
  await act(async () => { await fireEvent.press(screen.getByLabelText('Finish recording')); });
  expect(mockTranscribe).toHaveBeenCalledWith(CFG, 'w1', 'file:///answer.m4a');
  expect(screen.getByLabelText('Answer: Which funds?').props.value).toBe('CSPX and VUAA');
  expect(mockAnswer).not.toHaveBeenCalled();
  await fireEvent.changeText(screen.getByLabelText('Answer: Which funds?'), 'Only CSPX');
  await act(async () => { await fireEvent.press(screen.getByLabelText('Send answer')); });
  expect(mockAnswer).toHaveBeenCalledWith('w1', 'Only CSPX');
});

it('cancels on background, retains ownership until stopped and cannot submit an interrupted clip', async () => {
  let stopped!: () => void;
  mockRec.cancel.mockReturnValue(new Promise<void>((resolve) => { stopped = resolve; }));
  await render(<AnswerField />);
  await act(async () => { await fireEvent.press(screen.getByLabelText('Record answer')); });
  await act(() => onState('background'));
  expect(mockRec.cancel).toHaveBeenCalledTimes(1);
  await act(() => onState('active'));
  await fireEvent.press(screen.getByLabelText('Record answer'));
  expect(mockRec.start).toHaveBeenCalledTimes(1);
  await act(async () => { stopped(); });
  expect(mockTranscribe).not.toHaveBeenCalled(); expect(mockAnswer).not.toHaveBeenCalled();
});

it('discards a late transcription even if the app returns to foreground before it arrives', async () => {
  let heard!: (s: string) => void;
  mockTranscribe.mockReturnValue(new Promise<string>((resolve) => { heard = resolve; }));
  await render(<AnswerField />);
  await act(async () => { await fireEvent.press(screen.getByLabelText('Record answer')); });
  await fireEvent.press(screen.getByLabelText('Finish recording'));
  await waitFor(() => expect(mockTranscribe).toHaveBeenCalled());
  await act(() => { onState('background'); onState('active'); });
  await act(async () => { heard('Interrupted text'); });
  expect(screen.getByLabelText('Answer: Which funds?').props.value).toBe('');
  expect(mockAnswer).not.toHaveBeenCalled();
});

it('a late transcription cleanup cannot clear another screen\'s recording flag', async () => {
  let heard!: (s: string) => void;
  mockTranscribe.mockReturnValue(new Promise<string>((resolve) => { heard = resolve; }));
  const { unmount } = await render(<AnswerField />);
  await act(async () => { await fireEvent.press(screen.getByLabelText('Record answer')); });
  await fireEvent.press(screen.getByLabelText('Finish recording'));
  await waitFor(() => expect(mockTranscribe).toHaveBeenCalled());
  await unmount();
  mockApp.setRecording(true); // Another screen now owns the microphone.
  await act(async () => { heard('Too late'); });
  expect(mockApp.setRecording).toHaveBeenLastCalledWith(true);
});

it('pulses the working milestone only while visible, foregrounded and motion is allowed', async () => {
  mockReduced = false;
  const goal = detail();
  const { rerender, unmount } = await render(<MilestonePath goal={goal} onFile={jest.fn()} />);
  expect(mockRepeat).toHaveBeenCalledTimes(1);
  expect(screen.getByText('M1 · done')).toBeTruthy();
  await act(() => onState('background'));
  expect(mockRepeat).toHaveBeenCalledTimes(1);
  await act(() => onState('active'));
  expect(mockRepeat).toHaveBeenCalledTimes(2);
  mockReduced = true;
  await rerender(<MilestonePath goal={goal} onFile={jest.fn()} />);
  expect(mockRepeat).toHaveBeenCalledTimes(2);
  mockFocused = false; mockReduced = false;
  await rerender(<MilestonePath goal={goal} onFile={jest.fn()} />);
  expect(mockRepeat).toHaveBeenCalledTimes(2);
  const cancels = mockCancel.mock.calls.length;
  await unmount();
  expect(mockCancel.mock.calls.length).toBeGreaterThan(cancels);
});
