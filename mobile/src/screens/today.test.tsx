// The Today tab's screen. Kept outside src/app: Expo Router bundles every file there as a route.
import { act, fireEvent, render, screen, within } from '@testing-library/react-native';

import type { TodayBlock, TodayView } from '../chat/types';
import Today from '../app/(tabs)/today';
import { clearViewMemory } from '../core/useCachedView';

const mockApp = { cfg: { baseUrl: 'https://j', token: 't' } as { baseUrl: string; token: string } | null };
jest.mock('../state/AppContext', () => ({ useApp: () => mockApp }));
jest.mock('expo-router', () => ({
  useFocusEffect: (fn: () => void | (() => void)) => jest.requireActual('react').useEffect(fn, [fn]),
}));
jest.mock('react-native-screens/experimental', () => {
  const { View } = jest.requireActual('react-native');
  return { SafeAreaView: View };
});
jest.mock('react-native-safe-area-context', () => ({ useSafeAreaInsets: () => ({ top: 59, bottom: 34, left: 0, right: 0 }) }));
jest.mock('expo-haptics', () => ({
  selectionAsync: jest.fn(), notificationAsync: jest.fn(), NotificationFeedbackType: { Success: 'success' },
}));
jest.mock('expo-symbols', () => ({ SymbolView: ({ fallback }: { fallback: unknown }) => fallback }));

const mockGet = jest.fn();
const mockTap = jest.fn();
const mockDecide = jest.fn();
jest.mock('../chat/client', () => {
  class HttpError extends Error {
    status: number;
    constructor(status: number, message: string) {
      super(message);
      this.status = status;
    }
  }
  return {
    HttpError,
    getToday: (...a: unknown[]) => mockGet(...a),
    tapBlock: (...a: unknown[]) => mockTap(...a),
    decideApproval: (...a: unknown[]) => mockDecide(...a),
  };
});
const { HttpError } = jest.requireMock('../chat/client') as { HttpError: new (s: number, m: string) => Error };

const at = (h: number, m = 0, d = 6) => new Date(2026, 9, d, h, m).getTime() / 1000;
const CFG = { baseUrl: 'https://j', token: 't' };

const block = (over: Partial<TodayBlock> = {}): TodayBlock => ({
  uid: 'g', kind: 'gym', title: 'Gym', start: at(17, 30), end: at(18, 30), status: 'applied',
  outcome: null, started: false, offer: null, ...over,
});
const work = block({ uid: 'w', kind: 'work', title: 'Work', start: at(9), end: at(17), started: true });
const told = (outcome: 'done' | 'skipped' | 'partly') => ({ outcome, reason: '', actor: 'owner', at: at(14) });

const view = (over: Partial<TodayView> = {}): TodayView => ({
  date: '2026-10-06', label: 'Tuesday 6 Oct', now: at(14),
  blocks: [work, block()],
  yesterday: { date: '2026-10-05', label: 'Monday', blocks: [] },
  checkin: { enabled: true, time: '21:30', waiting: false, sent: false },
  reminders: [{ at: at(19), text: 'call the notary', repeat: 'once' }],
  tasks: [{ at: at(22), text: 'NVDA close', repeat: 'weekdays', skill: 'stock-research' }],
  week: ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].map((day, i) => ({
    date: `2026-10-${String(5 + i).padStart(2, '0')}`, day, planned: i < 5 ? 2 : 0,
    done: i === 0 ? 2 : 0, partly: 0, skipped: 0, moved: 0, unknown: 0,
  })),
  events: [{ title: 'Lunch with Karim', start: at(12, 30), end: at(13, 30), all_day: false, where: 'Café Kitsuné' }],
  events_note: '',
  ...over,
});

beforeEach(() => {
  clearViewMemory();
  jest.clearAllMocks();
  jest.spyOn(Date, 'now').mockReturnValue(at(14) * 1000);
  mockApp.cfg = { ...CFG };
  mockGet.mockResolvedValue(view());
});
afterEach(() => jest.restoreAllMocks());

const order = () => screen.getAllByText(/^(Work|Gym|Lunch with Karim|call the notary|NVDA close|now 14:00)$/)
  .map((n) => n.props.children);

it('shows the day in time order with a now line, the tally and the check-in', async () => {
  await render(<Today />);
  expect(await screen.findByText('Tuesday 6 Oct')).toBeTruthy();
  expect(order()).toEqual(['Work', 'Lunch with Karim', 'now 14:00', 'Gym', 'call the notary', 'NVDA close']);
  expect(screen.getByText('0 of 2 done')).toBeTruthy();
  expect(screen.getByText('Evening check-in at 21:30.')).toBeTruthy();
  expect(screen.getByText('12:30–13:30 · Café Kitsuné')).toBeTruthy();
  expect(screen.getByText('22:00 · timed check · stock-research')).toBeTruthy();
  expect(screen.getByText('in 3 h 30 min')).toBeTruthy();
  expect(within(screen.getByTestId('week-today')).getByText('6')).toBeTruthy();
});

it('offers every outcome on a started block and only Skipped on one still ahead', async () => {
  await render(<Today />);
  await screen.findByText('Tuesday 6 Oct');
  const w = within(screen.getByTestId('block-w'));
  expect(['Done', 'Partly', 'Skipped', 'Other time'].map((t) => w.queryByText(t) !== null)).toEqual([true, true, true, true]);
  const g = within(screen.getByTestId('block-g'));
  expect(g.queryByText('Done')).toBeNull();
  expect(g.getByText('Skip it')).toBeTruthy();
});

it('records a tap and shows the block as told, without repeating it in words', async () => {
  mockTap.mockResolvedValue({ text: 'Work 09:00-17:00: partly.', note: '', asked: [],
    today: view({ blocks: [{ ...work, outcome: told('partly') }, block()] }) });
  await render(<Today />);
  await screen.findByText('Tuesday 6 Oct');
  await act(async () => { await fireEvent.press(screen.getByLabelText('Work: Partly')); });
  expect(mockTap).toHaveBeenCalledWith(CFG, 'w', 'partly');
  const w = within(screen.getByTestId('block-w'));
  expect(w.queryByText('Work 09:00-17:00: partly.')).toBeNull();               // the badge says it
  expect(w.getByLabelText('Work: Partly. Change')).toBeTruthy();
  expect(w.queryByText('Done')).toBeNull();                                    // told: the choices fold away
});

it('a told block shows its outcome, and tapping it opens the choices again', async () => {
  mockGet.mockResolvedValue(view({ blocks: [{ ...work, outcome: told('done') }, block()] }));
  await render(<Today />);
  await screen.findByText('Tuesday 6 Oct');
  const w = within(screen.getByTestId('block-w'));
  expect(w.queryByText('Partly')).toBeNull();
  expect(w.queryByText(/ends in/)).toBeNull();                                // told: no countdown either
  expect(screen.getByText('1 of 2 done')).toBeTruthy();
  await fireEvent.press(w.getByLabelText('Work: Done. Change'));
  mockTap.mockResolvedValue({ text: 'Work 09:00-17:00: partly.', note: '', asked: [],
    today: view({ blocks: [{ ...work, outcome: told('partly') }, block()] }) });
  await act(async () => { await fireEvent.press(w.getByLabelText('Work: Partly')); });
  expect(mockTap).toHaveBeenCalledWith(CFG, 'w', 'partly');
  expect(w.getByLabelText('Work: Partly. Change')).toBeTruthy();
  expect(w.queryByLabelText('Work: Done')).toBeNull();                        // changed: folded again
});

it('says why a block could not take a tap, on that card', async () => {
  mockTap.mockRejectedValue(new HttpError(409, 'Gym 17:30-18:30 has not started yet'));
  await render(<Today />);
  await screen.findByText('Tuesday 6 Oct');
  await act(async () => { await fireEvent.press(screen.getByLabelText('Gym: Skip it')); });
  expect(within(screen.getByTestId('block-g')).getByText('Gym 17:30-18:30 has not started yet')).toBeTruthy();
});

it('a skip comes back with a new slot to approve on the card, and approving it moves the block off today', async () => {
  const offered = block({ outcome: told('skipped'), offer: { id: 12, label: 'Move it to Wednesday 07:45', why: 'you skipped it' } });
  mockTap.mockResolvedValue({ text: "Gym 17:30-18:30: skipped.\nMove Tuesday's gym (17:30-18:30) to Wednesday 07:45-08:45?",
    note: '', asked: [12], today: view({ blocks: [work, offered] }) });
  mockDecide.mockResolvedValue({ text: "Approved #12. Moved Tuesday's gym to Wednesday 07:45-08:45.", approval: null });
  await render(<Today />);
  await screen.findByText('Tuesday 6 Oct');
  await act(async () => { await fireEvent.press(screen.getByLabelText('Gym: Skip it')); });
  const offer = within(screen.getByTestId('offer-g'));
  expect(offer.getByText('Move it to Wednesday 07:45? (you skipped it)')).toBeTruthy();
  mockGet.mockResolvedValue(view({ blocks: [work] }));
  await act(async () => { await fireEvent.press(offer.getByLabelText('Approve: Move it to Wednesday 07:45')); });
  expect(mockDecide).toHaveBeenCalledWith(CFG, 12, true);
  expect(screen.queryByTestId('block-g')).toBeNull();
  expect(screen.getByText("Approved #12. Moved Tuesday's gym to Wednesday 07:45-08:45.")).toBeTruthy();   // not lost with the card
});

it('denying the new slot keeps the block where it was', async () => {
  const offered = block({ outcome: told('skipped'), offer: { id: 12, label: 'Move it to Wednesday 07:45', why: '' } });
  mockGet.mockResolvedValue(view({ blocks: [work, offered] }));
  mockDecide.mockResolvedValue({ text: 'Denied #12.', approval: null });
  await render(<Today />);
  await screen.findByText('Tuesday 6 Oct');
  mockGet.mockResolvedValue(view({ blocks: [work, { ...offered, offer: null }] }));
  await act(async () => { await fireEvent.press(screen.getByLabelText('Deny: Move it to Wednesday 07:45')); });
  expect(mockDecide).toHaveBeenCalledWith(CFG, 12, false);
  expect(screen.queryByTestId('offer-g')).toBeNull();
  expect(within(screen.getByTestId('block-g')).getByText('Denied #12.')).toBeTruthy();   // on its card
});

it("keeps yesterday's untold blocks open until 04:00", async () => {
  const late = block({ uid: 'mg', start: at(17, 30, 5), end: at(18, 30, 5), started: true });
  mockGet.mockResolvedValue(view({ yesterday: { date: '2026-10-05', label: 'Monday', blocks: [late] } }));
  mockTap.mockResolvedValue({ text: 'Gym 17:30-18:30: done.', note: '', asked: [], today: view() });
  await render(<Today />);
  const card = within(await screen.findByTestId('yesterday'));
  expect(card.getByText('Still open from Monday')).toBeTruthy();
  await act(async () => { await fireEvent.press(card.getByLabelText('Gym: Done')); });
  expect(mockTap).toHaveBeenCalledWith(CFG, 'mg', 'done');
  expect(screen.queryByTestId('yesterday')).toBeNull();
});

it('shows what the card cannot: a move made at once, or no free slot', async () => {
  const note = "Tuesday's gym (17:30-18:30): no free slot for it this week.";
  mockTap.mockResolvedValue({ text: `Gym 17:30-18:30: skipped.\n${note}`, note, asked: [],
    today: view({ blocks: [work, block({ outcome: told('skipped') })] }) });
  await render(<Today />);
  await screen.findByText('Tuesday 6 Oct');
  await act(async () => { await fireEvent.press(screen.getByLabelText('Gym: Skip it')); });
  expect(within(screen.getByTestId('block-g')).getByText(note)).toBeTruthy();
});

it('says when the calendar or the plan cannot be read, and when there is no plan', async () => {
  mockGet.mockResolvedValue(view({ blocks: [], events: null, events_note: 'Google Calendar is not connected on the server.' }));
  await render(<Today />);
  expect(await screen.findByText('Google Calendar is not connected on the server.')).toBeTruthy();
  expect(screen.getByText('No plan for today. Ask JARVIS to plan your week.')).toBeTruthy();
  expect(screen.queryByTestId('now-line')).toBeTruthy();
  await screen.unmount();
  mockGet.mockResolvedValue(view({ blocks: [], plan_error: 'The plan could not be read just now.' }));
  await render(<Today />);
  expect(await screen.findByText('The plan could not be read just now.')).toBeTruthy();
  expect(screen.queryByText(/No plan for today/)).toBeNull();
});

it('tells an older server apart from a refused token, and asks to pair when there is no server', async () => {
  mockGet.mockRejectedValue(new HttpError(404, 'Not Found'));
  await render(<Today />);
  expect(await screen.findByText(/doesn't have the Today view yet/)).toBeTruthy();
  await screen.unmount();
  mockGet.mockRejectedValue(Object.assign(new Error('mobile token required'), { status: 401 }));
  await render(<Today />);
  expect(await screen.findByText(/refused the token/)).toBeTruthy();
  await screen.unmount();
  mockApp.cfg = null;
  await render(<Today />);
  expect(screen.getByText('Pair with JARVIS in Settings first.')).toBeTruthy();
  await act(async () => undefined);
  expect(mockGet).toHaveBeenCalledTimes(2);                                   // unpaired: nothing fetched
});

it('reads the day again every minute while open, and the now line follows', async () => {
  jest.restoreAllMocks();
  jest.useFakeTimers({ now: at(14) * 1000 });
  try {
    await render(<Today />);
    await act(async () => undefined);
    expect(mockGet).toHaveBeenCalledTimes(1);
    jest.setSystemTime(at(15) * 1000);                                     // + the minute below: 15:01
    await act(async () => { jest.advanceTimersByTime(60_000); });
    expect(mockGet).toHaveBeenCalledTimes(2);
    expect(screen.getByText('now 15:01')).toBeTruthy();
  } finally {
    jest.useRealTimers();
  }
});
