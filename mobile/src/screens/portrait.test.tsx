import { act, render, screen } from '@testing-library/react-native';
import PortraitScreen from '../app/mind/portrait';
import type { PortraitView } from '../chat/types';
import { clearViewMemory } from '../core/useCachedView';

const mockApp = { cfg: { baseUrl: 'https://portrait.example', token: 'fixture' } };
const mockGet = jest.fn();
jest.mock('react-native-safe-area-context', () => ({ useSafeAreaInsets: () => ({ top: 59, bottom: 34 }) }));
jest.mock('../state/AppContext', () => ({ useApp: () => mockApp }));
jest.mock('../chat/client', () => ({ getPortrait: (...a: unknown[]) => mockGet(...a) }));
jest.mock('expo-router', () => ({ useFocusEffect: (fn: () => (() => void) | void) => jest.requireActual('react').useEffect(fn, [fn]) }));
jest.mock('../ui/ReadFrame', () => ({ ReadFrame: ({ children }: { children: unknown }) => children }));

const view = (extra: Partial<PortraitView> = {}): PortraitView => ({
  migrated: true, fading_days: 45,
  sections: [
    { title: 'Situation', entries: [{ text: 'Preparing for an exam', as_of: '2026-11-06' }, { text: 'Moving flat', as_of: '2026-09-20' }] },
    { title: 'Who they are', entries: [{ text: 'Builds a model railway', as_of: '2026-08-01' }] }],
  faded: [{ faded_on: '2026-11-20', entries: [{ text: 'Old plan B', as_of: '2026-10-03' }] },
    { faded_on: '2026-10-02', entries: [{ text: 'Old plan A', as_of: '2026-08-10' }] }],
  ...extra });
const flush = async () => { await act(async () => { await Promise.resolve(); await Promise.resolve(); }); };
beforeEach(() => {
  jest.useFakeTimers(); jest.setSystemTime(Date.parse('2026-11-07T12:00Z')); clearViewMemory();
  mockGet.mockReset().mockResolvedValue(view());
});
afterEach(() => jest.useRealTimers());

it('shows each section with its lines and dated as-of', async () => {
  await render(<PortraitScreen />); await flush();
  expect(screen.getByText('Situation')).toBeTruthy(); expect(screen.getByText('Who they are')).toBeTruthy();
  expect(screen.getByText('Preparing for an exam')).toBeTruthy(); expect(screen.getByText('as of 6 Nov')).toBeTruthy();
  expect(screen.getByText('as of 1 Aug')).toBeTruthy();
});
it('flags only old Situation lines as fading soon', async () => {
  await render(<PortraitScreen />); await flush();
  expect(screen.getAllByText(/fading soon/)).toHaveLength(1);
  expect(screen.getByText('as of 20 Sep · fading soon')).toBeTruthy();
});
it('lists faded lines newest first with their date range', async () => {
  await render(<PortraitScreen />); await flush();
  expect(screen.getByText('Faded')).toBeTruthy();
  const lines = screen.getAllByText(/faded \d+ (Nov|Oct)/).map((n) => n.props.children.join(''));
  expect(lines).toEqual(['Old plan B (3 Oct – faded 20 Nov)', 'Old plan A (10 Aug – faded 2 Oct)']);
});
it('says the portrait has not moved to the new format yet', async () => {
  mockGet.mockResolvedValue(view({ migrated: false, sections: [], faded: [] }));
  await render(<PortraitScreen />); await flush();
  expect(screen.getByText('Your portrait moves to the new format after your next conversation with JARVIS.')).toBeTruthy();
  expect(screen.queryByText('Faded')).toBeNull();
});
it('explains a server without the portrait view', async () => {
  mockGet.mockRejectedValue({ status: 404, message: 'not found' });
  await render(<PortraitScreen />); await flush();
  expect(screen.getByText('This server does not have the portrait view yet. Update it and restart JARVIS.')).toBeTruthy();
});
