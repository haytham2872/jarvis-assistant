import { act, fireEvent, render, screen } from '@testing-library/react-native';
import MindScreen from '../app/mind';
import { HubTiles } from '../hub/HubTiles';
import type { MindNode, MindView } from '../chat/types';
import { clearViewMemory } from '../core/useCachedView';

const mockApp = { cfg: { baseUrl: 'https://mind.example', token: 'fixture' } };
const mockGet = jest.fn(), mockHub = jest.fn(), mockPush = jest.fn();
let mockReduced = false;
const mockBlurs: Array<() => void> = [];
jest.mock('../state/AppContext', () => ({ useApp: () => mockApp }));
jest.mock('../chat/client', () => ({ getMind: (...a: unknown[]) => mockGet(...a), getHub: (...a: unknown[]) => mockHub(...a) }));
jest.mock('../chat/Orb', () => ({ useReduceMotion: () => mockReduced }));
jest.mock('expo-router', () => ({ router: { push: (...a: unknown[]) => mockPush(...a) },
  useFocusEffect: (fn: () => (() => void) | void) => jest.requireActual('react').useEffect(() => {
    const cleanup = fn(); if (cleanup) mockBlurs.push(cleanup); return cleanup; }, [fn]) }));
jest.mock('react-native-safe-area-context', () => ({ useSafeAreaInsets: () => ({ top: 59, bottom: 34 }) }));
jest.mock('../ui/ReadFrame', () => ({ ReadFrame: ({ children }: { children: unknown }) => children }));
jest.mock('../ui/skia', () => ({ skiaAvailable: () => false }));
jest.mock('../overnight/lastLooked', () => ({ useLastLooked: () => ({ loaded: true, at: null }) }));
jest.mock('expo-symbols', () => ({ SymbolView: ({ fallback }: { fallback: unknown }) => fallback }));
jest.mock('../goals/Screen', () => ({
  Button: ({ label, onPress, disabled }: { label: string; onPress: () => void; disabled: boolean }) => {
    const { Text, Pressable } = require('react-native'); return <Pressable accessibilityLabel={label} onPress={onPress} disabled={disabled}><Text>{label}</Text></Pressable>;
  }, Sheet: ({ title, children, onClose }: { title: string; children: unknown; onClose: () => void }) => {
    const { Text, View, Pressable } = require('react-native'); return <View testID="mind-sheet"><Text>{title}</Text><Pressable accessibilityLabel="Close sheet" onPress={onClose} />{children}</View>;
  },
}));
const now = Date.parse('2026-10-18T12:00Z') / 1000;
const node = (id: string, title: string, created: number): MindNode => ({ id, folder: id.split('/')[0], name: id.slice(id.indexOf('/') + 1), title,
  created, updated: created + 100, created_source: 'filename', size: 1, link: { folder: id.split('/')[0], name: id.slice(id.indexOf('/') + 1) } });
function view(extra: Partial<MindView> = {}): MindView {
  return { nodes: [node('topics/duration.md', 'Duration', now - 30 * 86400), node('people/WillTurner.md', 'Will Turner', now - 86400),
    { ...node('goals/gabc-compare/result.md', 'Compare bond funds', now - 10 * 86400), state: 'partial', created_source: 'modified' }],
    edges: [{ a: 'topics/duration.md', b: 'people/WillTurner.md', kind: 'mention' }], total: 3, truncated: false, notes: [], complete: true, ...extra };
}
const flush = async () => { await act(async () => { await Promise.resolve(); await Promise.resolve(); }); };
beforeEach(() => {
  jest.useFakeTimers(); jest.setSystemTime(now * 1000); clearViewMemory();
  mockGet.mockReset().mockResolvedValue(view()); mockHub.mockReset(); mockPush.mockReset();
  mockReduced = false; mockApp.cfg.baseUrl = 'https://mind.example'; mockBlurs.length = 0;
});
afterEach(() => jest.useRealTimers());

it('groups the fallback by folder, follows connections in the sheet and opens the exact goal result', async () => {
  await render(<MindScreen />); await flush();
  expect(screen.getByTestId('mind-list')).toBeTruthy();
  await fireEvent.press(screen.getByLabelText('Inspect Duration')); expect(screen.getByTestId('mind-sheet')).toBeTruthy();
  await fireEvent.press(screen.getByLabelText('Jump to Will Turner')); await fireEvent.press(screen.getByLabelText('Open note'));
  expect(mockPush).toHaveBeenLastCalledWith({ pathname: '/mind/note', params: view().nodes[1].link });
  expect(screen.queryByTestId('mind-sheet')).toBeNull(); await fireEvent.press(screen.getByLabelText('Inspect Compare bond funds'));
  expect(screen.getByText('Goal results · partial')).toBeTruthy(); expect(screen.getByText(/Creation not recorded/)).toBeTruthy();
  await fireEvent.press(screen.getByLabelText('Open note'));
  expect(mockPush).toHaveBeenLastCalledWith({ pathname: '/mind/note', params: { folder: 'goals', name: 'gabc-compare/result.md' } });
});
it('highlights search without losing the map, and filters both nodes and connections', async () => {
  await render(<MindScreen />); await flush(); await fireEvent.changeText(screen.getByLabelText('Find a note'), 'duration');
  expect(screen.getByText('1 matching titles · highlighted in the map and list')).toBeTruthy();
  expect(screen.getByLabelText('Inspect Will Turner')).toBeTruthy();
  await fireEvent.press(screen.getByLabelText('People')); expect(screen.queryByLabelText('Inspect Will Turner')).toBeNull();
  expect(screen.getByText('2 notes · 0 connections')).toBeTruthy();
  await fireEvent.press(screen.getByLabelText('People')); expect(screen.getByLabelText('Inspect Will Turner')).toBeTruthy();
});
it('keeps the slider working with Reduce Motion and separates it from the map surface', async () => {
  mockReduced = true; await render(<MindScreen />); await flush();
  const slider = screen.getByTestId('mind-time-slider');
  await fireEvent(slider, 'layout', { nativeEvent: { layout: { width: 300, height: 44 } } });
  await fireEvent(slider, 'responderGrant', { nativeEvent: { locationX: 0 } });
  expect(screen.getByLabelText('Inspect Duration')).toBeTruthy(); expect(screen.queryByLabelText('Inspect Will Turner')).toBeNull();
  expect(screen.getByText('1 note · 0 connections')).toBeTruthy();
  await fireEvent(slider, 'accessibilityAction', { nativeEvent: { actionName: 'increment' } });
  expect(screen.getByLabelText('Map date').props.accessibilityValue.now).toBeGreaterThan(view().nodes[0].created);
  await fireEvent(slider, 'responderMove', { nativeEvent: { locationX: 300 } });
  expect(screen.getByLabelText('Inspect Will Turner')).toBeTruthy();
});
it('plays growth, pauses on interaction, and cancels playback on unmount', async () => {
  const r = await render(<MindScreen />); await flush(); await fireEvent.press(screen.getByLabelText('Play growth'));
  expect(screen.queryByLabelText('Inspect Will Turner')).toBeNull();
  await act(() => { jest.advanceTimersByTime(12000); }); expect(screen.getByLabelText('Inspect Will Turner')).toBeTruthy();
  expect(screen.getByLabelText('Play growth')).toBeTruthy(); await r.unmount();
});
it('shows empty, incomplete and older-server states honestly', async () => {
  mockGet.mockResolvedValue(view({ nodes: [], edges: [], total: 0 })); const r = await render(<MindScreen />); await flush();
  expect(screen.getByText('A mind taking shape.')).toBeTruthy();
  mockGet.mockResolvedValue(view({ nodes: [], edges: [], total: 0, complete: false, notes: ['Goal results could not be read just now.'] }));
  await fireEvent.press(screen.getByLabelText('Refresh mind map')); await flush();
  expect(screen.getByText('The map is incomplete.')).toBeTruthy(); expect(screen.queryByText('A mind taking shape.')).toBeNull();
  mockApp.cfg.baseUrl = 'https://old.example'; mockGet.mockRejectedValue({ status: 404, message: 'Not Found' }); await r.rerender(<MindScreen />); await flush();
  expect(screen.getByText(/server does not have the mind map/)).toBeTruthy();
});
it('keeps surviving nodes beside failures and closes another server’s selected sheet', async () => {
  mockGet.mockResolvedValue(view({ complete: false, truncated: true, total: 330, notes: ['creative/broken.md could not be read just now.'] }));
  const r = await render(<MindScreen />); await flush(); await fireEvent.press(screen.getByLabelText('Inspect Duration'));
  expect(screen.getByText('3 notes of 330 · 1 connection')).toBeTruthy(); expect(screen.getByText(/Older letters with no connection/)).toBeTruthy(); expect(screen.getByText('creative/broken.md could not be read just now.')).toBeTruthy();
  mockApp.cfg.baseUrl = 'https://other.example'; mockGet.mockImplementation(() => new Promise(() => {})); await r.rerender(<MindScreen />); await flush();
  expect(screen.queryByTestId('mind-sheet')).toBeNull(); expect(screen.queryByLabelText('Inspect Duration')).toBeNull();
});
it('adds a hub tile only when the independent part is present', async () => {
  mockHub.mockResolvedValue({ mind: { count: 260, total: 330, complete: true } }); await render(<HubTiles />); await flush();
  await fireEvent.press(screen.getByLabelText('Mind, 260 notes of 330')); expect(mockPush).toHaveBeenCalledWith('/mind');
});
it('stops replaying growth when he leaves the screen', async () => {
  await render(<MindScreen />); await flush(); await fireEvent.press(screen.getByLabelText('Play growth'));
  await act(() => { jest.advanceTimersByTime(360); });
  await act(() => { mockBlurs.splice(0).forEach((blur) => blur()); });
  await act(() => { jest.advanceTimersByTime(12000); });
  expect(screen.queryByLabelText('Inspect Will Turner')).toBeNull();
});
