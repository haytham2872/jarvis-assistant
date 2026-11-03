// The Activity tab's screen. Kept outside src/app: Expo Router bundles every file there as a route.
import { act, fireEvent, render, screen } from '@testing-library/react-native';

import type { ActivityView } from '../chat/types';
import Activity from '../app/(tabs)/activity';
import { clearViewMemory } from '../core/useCachedView';

const mockApp = { cfg: { baseUrl: 'https://j', token: 't' } as { baseUrl: string; token: string } | null, waitingCount: 0 };
const mockPush = jest.fn();
jest.mock('../state/AppContext', () => ({ useApp: () => mockApp }));
jest.mock('expo-router', () => ({
  useFocusEffect: (fn: () => void | (() => void)) => jest.requireActual('react').useEffect(fn, [fn]),
  router: { push: (...args: unknown[]) => mockPush(...args) },
}));
jest.mock('react-native-screens/experimental', () => {
  const { View } = jest.requireActual('react-native');
  return { SafeAreaView: View };
});
jest.mock('react-native-safe-area-context', () => ({ useSafeAreaInsets: () => ({ top: 59, bottom: 34, left: 0, right: 0 }) }));
jest.mock('expo-haptics', () => ({ notificationAsync: jest.fn(), NotificationFeedbackType: { Success: 'success' } }));

const mockGet = jest.fn();
const mockPause = jest.fn();
const mockResume = jest.fn();
const mockHub = jest.fn();
jest.mock('../chat/client', () => ({
  getActivity: (...a: unknown[]) => mockGet(...a),
  pauseWork: (...a: unknown[]) => mockPause(...a),
  resumeWork: (...a: unknown[]) => mockResume(...a),
  getHub: (...a: unknown[]) => mockHub(...a),
}));

const view = (over: Partial<ActivityView> = {}): ActivityView => ({
  as_of: 1_760_000_000, paused: null,
  sections: [
    { title: 'Running now', items: ['job #3 (2m): research TTWO'], empty: 'nothing is running.' },
    { title: 'Scheduled', items: [], empty: 'nothing is scheduled.' },
  ],
  ...over,
});

beforeEach(() => {
  clearViewMemory();
  jest.clearAllMocks();
  mockApp.cfg = { baseUrl: 'https://j', token: 't' };
  mockGet.mockResolvedValue(view());
  mockHub.mockResolvedValue({ goals: { enabled: true, active: 2, questions: 1, proposals: 1 } });
});

it('opens Goals from the live-count hub above the existing activity', async () => {
  await render(<Activity />);
  await fireEvent.press(await screen.findByLabelText('Goals, 2 active · 1 question · 1 offered'));
  expect(mockPush).toHaveBeenCalledWith('/goals');
  expect(screen.getByText('Running now')).toBeTruthy();
  expect(screen.queryByText('While you slept')).toBeNull();
});

it('shows each section of /activity, and says so when one is empty', async () => {
  await render(<Activity />);
  expect(await screen.findByText('job #3 (2m): research TTWO')).toBeTruthy();
  expect(screen.getByText('Running now')).toBeTruthy();
  expect(screen.getByText('Nothing is scheduled.')).toBeTruthy();
});

it('pauses for the chosen time, holding reminders only when asked', async () => {
  const paused = view({ paused: 'Paused until 16:00 Paris time (work only).' });
  mockPause.mockResolvedValue({ text: 'Paused until 16:00 Paris time.', activity: paused });
  await render(<Activity />);
  await screen.findByText('Pause my own work');
  await act(async () => { await fireEvent.press(screen.getByLabelText('Pause for 3 hours')); });
  expect(mockPause).toHaveBeenCalledWith({ baseUrl: 'https://j', token: 't' }, 3, false);
  expect(screen.getByText('Paused until 16:00 Paris time (work only).')).toBeTruthy();
  expect(screen.getByText('Resume')).toBeTruthy();
});

it('"hold reminders too" pauses everything until resumed', async () => {
  mockPause.mockResolvedValue({ text: 'Paused.', activity: view({ paused: 'Paused (everything).' }) });
  await render(<Activity />);
  await screen.findByText('Pause my own work');
  await fireEvent(screen.getByLabelText('Hold reminders and the brief too'), 'valueChange', true);
  await act(async () => { await fireEvent.press(screen.getByLabelText('Pause for Until I resume')); });
  expect(mockPause).toHaveBeenCalledWith(expect.anything(), null, true);
});

it('resumes and shows what the pause skipped', async () => {
  mockGet.mockResolvedValue(view({ paused: 'Paused since 14:00 Paris time.' }));
  mockResume.mockResolvedValue({ text: 'Resumed. While paused:\n- skipped scheduled check: NVDA open', activity: view() });
  await render(<Activity />);
  const resume = await screen.findByText('Resume');
  await act(async () => { await fireEvent.press(resume); });
  expect(mockResume).toHaveBeenCalledTimes(1);
  expect(screen.getByText(/skipped scheduled check: NVDA open/)).toBeTruthy();
  expect(screen.getByText('Pause my own work')).toBeTruthy();
});

it('says why it could not read the view, and asks to pair when there is no server', async () => {
  mockGet.mockRejectedValue(Object.assign(new Error('mobile token required'), { status: 401 }));
  await render(<Activity />);
  expect(await screen.findByText(/refused the token/)).toBeTruthy();
  await screen.unmount();
  mockApp.cfg = null;
  await render(<Activity />);
  expect(screen.getByText('Pair with JARVIS in Settings first.')).toBeTruthy();
  await act(async () => undefined);
  expect(screen.queryByText(/Can't reach|refused|JARVIS answered/)).toBeNull();   // unpaired is not an error
  expect(mockGet).toHaveBeenCalledTimes(1);
});
