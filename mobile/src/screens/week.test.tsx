import { act, fireEvent, render, screen } from '@testing-library/react-native';
import type { WeekView as Data } from '../chat/types';
import { WeekView } from '../week/WeekView';
import { instant } from '../week/model';
import { clearViewMemory } from '../core/useCachedView';

const mockApp = { cfg: { baseUrl: 'https://week', token: 't' } };
const mockGet = jest.fn(), mockMove = jest.fn(), mockUndo = jest.fn(), mockApply = jest.fn(), mockAdd = jest.fn();
jest.mock('../state/AppContext', () => ({ useApp: () => mockApp }));
jest.mock('expo-router', () => ({ useFocusEffect: (fn: () => unknown) => jest.requireActual('react').useEffect(fn, [fn]) }));
jest.mock('react-native-screens/experimental', () => ({ SafeAreaView: (p: unknown) => { const { View } = require('react-native'); return <View {...p as object} />; } }));
jest.mock('react-native-safe-area-context', () => ({ useSafeAreaInsets: () => ({ top: 59, bottom: 34 }) }));
jest.mock('../chat/Orb', () => ({ useReduceMotion: () => false }));
jest.mock('../goals/Screen', () => ({
  explainGoal: (e: Error) => e.message,
  Button: ({ label, accessibilityLabel, onPress, disabled }: { label: string; accessibilityLabel: string; onPress: () => void; disabled: boolean }) => {
    const { Pressable, Text } = require('react-native');
    return <Pressable accessibilityRole="button" accessibilityLabel={accessibilityLabel ?? label} disabled={disabled} onPress={onPress}><Text>{label}</Text></Pressable>;
  },
  Sheet: ({ children }: { children: unknown }) => children,
}));
jest.mock('../chat/client', () => ({ getWeek: (...a: unknown[]) => mockGet(...a), moveWeekBlock: (...a: unknown[]) => mockMove(...a),
  undoWeek: (...a: unknown[]) => mockUndo(...a), applyWeek: (...a: unknown[]) => mockApply(...a), addWeekBlock: (...a: unknown[]) => mockAdd(...a) }));
jest.mock('../week/GestureSurface', () => ({
  DragBlock: (p: { uid: string; onDrop: (uid: string, index: number, minute: number) => void; children: unknown }) => {
    const { Pressable, Text, View } = require('react-native');
    return <View><Pressable accessibilityLabel={`Drag ${p.uid}`} onPress={() => p.onDrop(p.uid, 1, 600)}><Text>drag</Text></Pressable>{p.children}</View>;
  }, EmptySurface: ({ children }: { children: unknown }) => children,
}));
const week = '2026-10-12';
function view(): Data {
  return { week, start: week, now: instant(week, 540), plan_id: 1, notes: [], draft: null,
    rules: { kinds: [{ kind: 'gym', minutes: 60, addable: true, start: '07:00', end: '22:00' }], hours: { start: '07:00', end: '22:00' }, buffer: 0 },
    days: [week, '2026-10-13', '2026-10-14'].map((date, i) => ({ date, label: ['Mon 12', 'Tue 13', 'Wed 14'][i],
      blocks: i ? [] : [{ uid: 'g', title: 'Gym', kind: 'gym', start: instant(date, 540), end: instant(date, 600), outcome: 'done', status: 'scheduled' }],
      events: i ? [] : [{ id: 'e', title: 'Dentist', start: instant(date, 840), end: instant(date, 900), all_day: false }],
      events_note: '', counts: { done: i ? 0 : 1, partly: 0, skipped: 0, planned: i ? 0 : 1 } })),
  };
}
const flush = async () => { await act(async () => { await Promise.resolve(); await Promise.resolve(); }); };
beforeEach(() => {
  jest.useFakeTimers(); jest.setSystemTime(new Date('2026-10-12T08:00:00Z')); clearViewMemory();
  mockGet.mockReset().mockResolvedValue(view()); mockMove.mockReset(); mockApply.mockReset(); mockUndo.mockReset(); mockAdd.mockReset();
});
afterEach(() => { jest.useRealTimers(); });

it('renders fixed calendar, outcome icons and counts beside movable blocks', async () => {
  await render(<WeekView onDay={jest.fn()} />); await flush();
  expect(screen.getByText('Dentist')).toBeTruthy(); expect(screen.getByText('Gym ✓')).toBeTruthy();
  expect(screen.getByText('1 done · 0 partly')).toBeTruthy();
  expect(screen.queryByLabelText(/Move Dentist/)).toBeNull();
});
it('moves optimistically, shows saving, then offers Undo for six seconds', async () => {
  let resolve!: (r: unknown) => void; mockMove.mockReturnValue(new Promise((r) => { resolve = r; }));
  await render(<WeekView onDay={jest.fn()} />); await flush();
  await fireEvent.press(screen.getByLabelText('Drag g'));
  expect(screen.getByLabelText('Saving block')).toBeTruthy(); expect(screen.getByText('10:00')).toBeTruthy();
  expect(mockMove).toHaveBeenCalledWith(mockApp.cfg, 'g', { view_start: week, days: 3, plan_id: 1 }, '2026-10-13', '10:00');
  const updated = view(); updated.plan_id = 2;
  await act(async () => resolve({ week: updated, warning: '', text: 'Updated.' }));
  expect(screen.getByLabelText('Undo')).toBeTruthy();
  await act(() => jest.advanceTimersByTime(5999)); expect(screen.getByLabelText('Undo')).toBeTruthy();
  await act(() => jest.advanceTimersByTime(1)); expect(screen.queryByLabelText('Undo')).toBeNull();
});
it('rolls back a refusal and displays its actual reason', async () => {
  mockMove.mockRejectedValue(new Error('That overlaps your calendar.'));
  await render(<WeekView onDay={jest.fn()} />); await flush();
  mockGet.mockRejectedValue(new Error('Network still down.'));
  await fireEvent.press(screen.getByLabelText('Drag g')); await flush();
  expect(screen.getByText('That overlaps your calendar.')).toBeTruthy();
  expect(screen.getByText('09:00')).toBeTruthy(); expect(screen.queryByLabelText('Undo')).toBeNull();
});
it('pins Undo to the returned version and applies only the displayed proposal', async () => {
  const v = view(); v.plan_id = 2; mockMove.mockResolvedValue({ week: v, warning: '', text: 'Updated.' });
  mockUndo.mockResolvedValue({ week: view(), warning: '', text: 'Undone.' });
  const r = await render(<WeekView onDay={jest.fn()} />); await flush(); await fireEvent.press(screen.getByLabelText('Drag g')); await flush();
  await fireEvent.press(screen.getByLabelText('Undo')); await flush();
  expect(mockUndo).toHaveBeenCalledWith(mockApp.cfg, { view_start: week, days: 3, plan_id: 2 });
  await r.unmount(); clearViewMemory();
  const draft = view(); draft.draft = { week, plan_id: 3, blocks: [{ ...draft.days[0].blocks[0], uid: 'new', start: instant(week, 660), end: instant(week, 720) }] };
  mockGet.mockResolvedValue(draft); mockApply.mockResolvedValue({ week: view(), warning: '', text: 'Applied.' });
  await render(<WeekView onDay={jest.fn()} />); await flush(); expect(screen.getByTestId('ghost-new')).toBeTruthy();
  await fireEvent.press(screen.getByLabelText('Apply proposal')); await flush();
  expect(mockApply).toHaveBeenCalledWith(mockApp.cfg, { view_start: week, days: 3, plan_id: 1 }, week, 3);
  expect(screen.queryByTestId('ghost-new')).toBeNull();
});
it('reviews a new block and submits its named kind, exact time and duration', async () => {
  mockAdd.mockResolvedValue({ week: view(), warning: '', text: 'Added.' });
  await render(<WeekView onDay={jest.fn()} />); await flush(); await fireEvent.press(screen.getByLabelText('Add block'));
  await fireEvent.changeText(screen.getByLabelText('Block start time'), '15:15');
  await fireEvent.changeText(screen.getByLabelText('Block duration in minutes'), '45');
  await fireEvent.press(screen.getByLabelText('Save block')); await flush();
  expect(mockAdd).toHaveBeenCalledWith(mockApp.cfg, { view_start: week, days: 3, plan_id: 1 }, week, '15:15', 'gym', 45);
});

it('does not offer Undo for a no-op move or a partial calendar failure', async () => {
  mockMove.mockResolvedValue({ week: view(), warning: '', text: 'Already at that time.' });
  await render(<WeekView onDay={jest.fn()} />); await flush(); await fireEvent.press(screen.getByLabelText('Drag g')); await flush();
  expect(screen.getByText('Already at that time.')).toBeTruthy(); expect(screen.queryByLabelText('Undo')).toBeNull();
  const v = view(); v.plan_id = 2;
  mockMove.mockResolvedValue({ week: v, warning: 'Calendar refused the move.', text: 'Calendar refused the move.' });
  await fireEvent.press(screen.getByLabelText('Drag g')); await flush();
  expect(screen.getByText('Calendar refused the move.')).toBeTruthy(); expect(screen.queryByLabelText('Undo')).toBeNull();
});

it('does not persist an unconfirmed drag as a saved position after leaving the screen', async () => {
  let resolve!: (r: unknown) => void;
  mockMove.mockReturnValue(new Promise((r) => { resolve = r; }));
  const first = await render(<WeekView onDay={jest.fn()} />); await flush();
  await fireEvent.press(screen.getByLabelText('Drag g')); expect(screen.getByText('10:00')).toBeTruthy(); await first.unmount();
  mockGet.mockRejectedValue(new Error('Offline.'));
  await render(<WeekView onDay={jest.fn()} />); await flush();
  expect(screen.getByText('09:00')).toBeTruthy();
  await act(async () => resolve({ week: view(), text: 'Updated.', warning: '' }));
  expect(screen.getByText('09:00')).toBeTruthy();
});
