import { act, fireEvent, render, screen } from '@testing-library/react-native';
import OvernightScreen from '../app/overnight';
import NoteScreen from '../app/mind/note';
import { clearViewMemory } from '../core/useCachedView';
import { HubTiles } from '../hub/HubTiles';
import type { OvernightItem, OvernightView } from '../chat/types';

const mockGet = jest.fn(), mockNote = jest.fn(), mockHub = jest.fn(), mockSave = jest.fn(), mockPush = jest.fn();
const mockApp = { cfg: { baseUrl: 'https://overnight.example', token: 'fixture' } };
let mockLooked: number | null = null;
let mockParams = { folder: 'topics', name: 'bond-prices.md' };
jest.mock('../state/AppContext', () => ({ useApp: () => mockApp }));
jest.mock('expo-router', () => ({ router: { push: (...a: unknown[]) => mockPush(...a), canGoBack: () => true, back: jest.fn() },
  useLocalSearchParams: () => mockParams,
  useFocusEffect: (fn: () => unknown) => jest.requireActual('react').useEffect(fn, [fn]) }));
jest.mock('../overnight/lastLooked', () => ({ useLastLooked: () => ({ at: mockLooked, loaded: true }), saveLooked: (...a: unknown[]) => mockSave(...a) }));
jest.mock('react-native-safe-area-context', () => ({ useSafeAreaInsets: () => ({ top: 59, bottom: 34 }) }));
jest.mock('../chat/client', () => ({ getOvernight: (...a: unknown[]) => mockGet(...a), getVaultNote: (...a: unknown[]) => mockNote(...a), getHub: (...a: unknown[]) => mockHub(...a) }));
jest.mock('../ui/ReadFrame', () => ({ ReadFrame: ({ children }: { children: unknown }) => children }));
jest.mock('../goals/Screen', () => ({ Button: ({ label, onPress }: { label: string; onPress: () => void }) => {
  const { Text, Pressable } = require('react-native'); return <Pressable accessibilityLabel={label} onPress={onPress}><Text>{label}</Text></Pressable>;
} }));
jest.mock('expo-symbols', () => ({ SymbolView: ({ fallback }: { fallback: unknown }) => fallback }));
const now = Date.parse('2026-10-25T07:20Z') / 1000;
const quiet = (extra: Partial<OvernightView> = {}): OvernightView => ({ as_of: now, since: now - 40800, items: [], complete: true, notes: [], ...extra });
const example = (extra: Partial<OvernightItem> = {}): OvernightItem => ({ id: 'research:1', at: now - 3600, kind: 'research', title: 'Bond prices [counterpoint] (queued)', detail: 'wrote topics/bond-prices.md — separated duration from credit risk',
  link: { folder: 'topics', name: 'bond-prices.md' }, ok: null, ...extra });
const flush = async () => { await act(async () => { await Promise.resolve(); await Promise.resolve(); }); };
beforeEach(() => {
  jest.useFakeTimers(); jest.setSystemTime(now * 1000); clearViewMemory();
  [mockGet, mockNote, mockHub, mockSave, mockPush].forEach((m) => m.mockReset());
  mockLooked = null; mockApp.cfg.baseUrl = 'https://overnight.example'; mockParams = { folder: 'topics', name: 'bond-prices.md' };
});
afterEach(() => jest.useRealTimers());

it('makes a quiet night intentional and never advances last-looked on open or an accidental close', async () => {
  mockGet.mockResolvedValue(quiet()); const r = await render(<OvernightScreen />); await flush();
  expect(screen.getByText('A quiet night.')).toBeTruthy(); expect(screen.getByText(/Nothing to catch up on/)).toBeTruthy();
  expect(mockSave).not.toHaveBeenCalled(); await r.unmount(); expect(mockSave).not.toHaveBeenCalled();
});
it('saves on leaving after a meaningful visit, through the snapshot actually read', async () => {
  mockGet.mockResolvedValue(quiet({ items: [example()] })); const r = await render(<OvernightScreen />); await flush();
  expect(mockSave).not.toHaveBeenCalled(); await act(() => { jest.advanceTimersByTime(4000); }); await r.unmount();
  expect(mockSave).toHaveBeenCalledWith(expect.stringContaining('overnight-looked'), now);
});
it('can mark a quiet cached response seen after a fresh 304 validation', async () => {
  mockGet.mockResolvedValue(quiet({ as_of: now - 600 }));
  const r = await render(<OvernightScreen />); await flush(); await act(() => { jest.advanceTimersByTime(4000); }); await r.unmount();
  expect(mockSave).toHaveBeenCalledWith(expect.any(String), now);
});
it('shows a source failure beside surviving work and never says quiet or marks it read', async () => {
  mockGet.mockResolvedValue(quiet({ complete: false, notes: ['Research passes could not be read just now.'],
    items: [example({ id: 'watch:1', kind: 'watch', title: 'A launch delay', detail: 'The operator announced upper-level winds.', link: null })] }));
  const r = await render(<OvernightScreen />); await flush();
  expect(screen.getByText('Part of the night')).toBeTruthy(); expect(screen.getByText('Research passes could not be read just now.')).toBeTruthy();
  expect(screen.getByText('A launch delay')).toBeTruthy(); expect(screen.queryByText('A quiet night.')).toBeNull();
  await act(() => { jest.advanceTimersByTime(4000); }); await r.unmount(); expect(mockSave).not.toHaveBeenCalled();
});
it('changes the requested period, uses the saved marker and opens a note', async () => {
  mockLooked = now - 45000; mockGet.mockResolvedValue(quiet({ items: [example()] }));
  await render(<OvernightScreen />); await flush(); expect(mockGet.mock.calls[0][1]).toBeNull();
  await fireEvent.press(screen.getByLabelText('Last 24 h')); await flush(); expect(mockGet.mock.calls.at(-1)[1]).toBe(now - 86400);
  await fireEvent.press(screen.getByLabelText('Since I last looked')); await flush(); expect(mockGet.mock.calls.at(-1)[1]).toBe(mockLooked);
  await fireEvent.press(screen.getByLabelText(`Open Research pass: ${example().title}`));
  expect(mockPush).toHaveBeenCalledWith({ pathname: '/mind/note', params: example().link });
});
it('handles first visit, older servers and failed requests without a quiet-night claim', async () => {
  mockGet.mockRejectedValue({ status: 404, message: 'Not Found' }); await render(<OvernightScreen />); await flush();
  await fireEvent.press(screen.getByLabelText('Since I last looked')); await flush();
  expect(screen.getByText('No previous visit saved. Showing last evening.')).toBeTruthy();
  expect(screen.getByText(/server does not have the overnight view/)).toBeTruthy();
  expect(screen.queryByText('A quiet night.')).toBeNull(); expect(mockSave).not.toHaveBeenCalled();
});
it('does not show or dismiss another account’s cached night on a scope change', async () => {
  mockGet.mockResolvedValue(quiet({ items: [example()] })); const r = await render(<OvernightScreen />); await flush();
  mockGet.mockImplementation(() => new Promise(() => {})); await act(() => { jest.advanceTimersByTime(4000); });
  mockApp.cfg.baseUrl = 'https://other.example'; await r.rerender(<OvernightScreen />); await flush();
  expect(screen.queryByText(example().title)).toBeNull(); expect(mockSave).not.toHaveBeenCalled();
});
it('shows the optional hub tile with counts from the last-looked window', async () => {
  mockLooked = now - 4000; mockHub.mockResolvedValue({ overnight: { since: mockLooked, count: 2, complete: true } });
  await render(<HubTiles />); await flush(); expect(mockHub.mock.calls[0][2]).toBe(mockLooked);
  await fireEvent.press(screen.getByLabelText(/^While you slept,/)); expect(mockPush).toHaveBeenCalledWith('/overnight');
});
it('renders long Markdown with virtualized blocks, a folder header and the modified date', async () => {
  const text = '# Bond prices\n\n' + Array.from({ length: 500 }, (_, i) => `## Observation ${i}\n\nDuration is not credit risk. Check the issuer and the rate path.\n\n`).join('');
  mockNote.mockResolvedValue({ folder: 'topics', name: 'bond-prices.md', text: text.slice(0, 60000), modified: now - 3600, truncated: true });
  await render(<NoteScreen />); await flush(); expect(screen.getByText('topics / bond-prices.md')).toBeTruthy();
  expect(screen.getByText(/Modified 25 Oct/)).toBeTruthy(); expect(screen.getByText(/first 60,000/)).toBeTruthy();
  expect(screen.queryByText('Observation 499')).toBeNull(); // offscreen blocks are not all mounted
  expect(screen.getByTestId('note-blocks').props.initialNumToRender).toBe(6);
  expect(screen.getByText('Observation 0')).toBeTruthy();
});
it('explains a missing note and discards the previous note when the route changes', async () => {
  mockNote.mockResolvedValue({ folder: 'topics', name: 'bond-prices.md', text: '# Duration\n\nA first note.', modified: now, truncated: false });
  const r = await render(<NoteScreen />); await flush(); expect(screen.getByText('A first note.')).toBeTruthy();
  mockNote.mockRejectedValue({ status: 404, message: 'That note no longer exists.' }); mockParams = { folder: 'letters', name: '2026-10-22_8f3b162a.md' };
  await r.rerender(<NoteScreen />); await flush(); expect(screen.queryByText('A first note.')).toBeNull();
  expect(screen.getByText('That note no longer exists.')).toBeTruthy();
});
