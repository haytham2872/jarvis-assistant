import { act, fireEvent, render, screen } from '@testing-library/react-native';
import RecordScreen from '../app/record';
import { clearViewMemory } from '../core/useCachedView';
import { call, empty, thin } from '../record/fixtures';
import { HubTiles } from '../hub/HubTiles';

const mockGet = jest.fn(), mockHub = jest.fn(), mockPush = jest.fn();
const mockApp = { cfg: { baseUrl: 'https://record.example', token: 'test' } };
jest.mock('../state/AppContext', () => ({ useApp: () => mockApp }));
jest.mock('expo-router', () => ({ router: { push: (...a: unknown[]) => mockPush(...a), canGoBack: () => true, back: jest.fn() },
  useFocusEffect: (fn: () => unknown) => jest.requireActual('react').useEffect(fn, [fn]) }));
jest.mock('react-native-safe-area-context', () => ({ useSafeAreaInsets: () => ({ top: 59, bottom: 34 }) }));
jest.mock('../chat/client', () => ({ getRecord: (...a: unknown[]) => mockGet(...a), getHub: (...a: unknown[]) => mockHub(...a) }));
jest.mock('../goals/Screen', () => ({
  Screen: ({ children, error, loading }: { children: unknown; error: string; loading: boolean }) => {
    const { View, Text } = require('react-native'); return <View>{loading ? <Text>Loading track record</Text> : null}{error ? <Text>{error}</Text> : null}{children}</View>;
  }, Sheet: ({ children, title, onClose }: { children: unknown; title: string; onClose: () => void }) => {
    const { View, Text, Pressable } = require('react-native'); return <View testID="call-sheet"><Text>{title}</Text>{children}<Pressable accessibilityLabel="Close sheet" onPress={onClose} /></View>;
  }, Button: ({ label, onPress }: { label: string; onPress: () => void }) => { const { Pressable, Text } = require('react-native'); return <Pressable accessibilityRole="button" accessibilityLabel={label} onPress={onPress}><Text>{label}</Text></Pressable>; },
}));
jest.mock('expo-symbols', () => ({ SymbolView: ({ fallback }: { fallback: unknown }) => fallback }));
const flush = async () => { await act(async () => { await Promise.resolve(); await Promise.resolve(); }); };
beforeEach(() => { jest.useFakeTimers(); clearViewMemory(); mockGet.mockReset(); mockHub.mockReset(); mockPush.mockClear(); mockApp.cfg.baseUrl = 'https://record.example'; });
afterEach(() => jest.useRealTimers());

it('makes an empty record useful without zero scores, blank axes or a misleading trend', async () => {
  mockGet.mockResolvedValue(empty()); await render(<RecordScreen />); await flush();
  expect(screen.getByText('No forecasts yet')).toBeTruthy(); expect(screen.getByText('No forecasts logged yet.')).toBeTruthy();
  expect(screen.queryByText('Brier score')).toBeNull(); expect(screen.queryByTestId('record-months')).toBeNull();
  expect(screen.getByText('Waiting for outcomes')).toBeTruthy();
  expect(screen.queryByText('Calibration')).toBeNull();
});
it('shows open calls and their check sheet with no settled score', async () => {
  const c = call({ outcome: null, resolved_at: null, resolution_quote: '', resolver: '' });
  mockGet.mockResolvedValue(empty({ open: [c], text: 'Track record: 0 resolved forecast(s), 1 open. Nothing has resolved yet, so there is no score to report.' }));
  await render(<RecordScreen />); await flush(); expect(screen.getByText('No score yet')).toBeTruthy();
  await fireEvent.press(screen.getByLabelText(`Open forecast F17: ${c.claim}`));
  expect(screen.getByText(`True if: ${c.criteria}`)).toBeTruthy(); expect(screen.getByText(c.check_query)).toBeTruthy();
  expect(screen.queryByText('What settled it')).toBeNull();
});
it('puts the thin-record warning first and switches both scores and calibration data', async () => {
  mockGet.mockResolvedValue(thin()); await render(<RecordScreen />); await flush();
  expect(screen.getByText('Too early to tell')).toBeTruthy(); expect(screen.getByText(thin().caution)).toBeTruthy();
  expect(screen.getAllByText('0.109')).toHaveLength(3); expect(screen.getByText(/Said 67% on average/)).toBeTruthy();
  await fireEvent.press(screen.getByLabelText('Gut')); expect(screen.getAllByText('0.072')).toHaveLength(3);
  expect(screen.getByText(/Said 73% on average/)).toBeTruthy();
});
it('opens a settled call with both numbers, the criterion, resolver, quote and saved query', async () => {
  mockGet.mockResolvedValue(thin()); await render(<RecordScreen />); await flush();
  await fireEvent.press(screen.getByLabelText(`Open forecast F17: ${call().claim}`));
  expect(screen.getByText('Gut 73% · Corrected 67%')).toBeTruthy(); expect(screen.getByText(call().resolution_quote)).toBeTruthy();
  expect(screen.getByText(`True if: ${call().criteria}`)).toBeTruthy();
  expect(screen.getByText(/settled via web/)).toBeTruthy();
  await fireEvent.press(screen.getByLabelText('Close sheet')); expect(screen.queryByTestId('call-sheet')).toBeNull();
});
it('says unavailable rather than empty after a failed read or calculation', async () => {
  mockGet.mockResolvedValue(empty({ available: false, text: 'The forecast record could not be read just now.', notes: ['Storage unavailable.'] }));
  await render(<RecordScreen />); await flush(); expect(screen.getByText('Record unavailable')).toBeTruthy();
  expect(screen.getByText('Storage unavailable.')).toBeTruthy(); expect(screen.queryByText('No forecasts yet')).toBeNull();
});
it('keeps the hub optional and uses the server flag for a too-early tile', async () => {
  mockHub.mockResolvedValue({ goals: { active: 0, questions: 0, proposals: 0, enabled: true } });
  const r = await render(<HubTiles />); await flush(); expect(screen.queryByText('Track record')).toBeNull(); await r.unmount(); clearViewMemory();
  mockHub.mockResolvedValue({ record: { settled: 1, brier: .1089, early: true } });
  await render(<HubTiles />); await flush(); await fireEvent.press(screen.getByLabelText('Track record, 1 settled · Too early to tell'));
  expect(mockPush).toHaveBeenCalledWith('/record');
});

it('uses the shared month line and closes a call when changing server/account scope', async () => {
  const v = thin(); v.by_month.unshift({ month: '2026-09', n: 1, brier: .238144, brier_gut: .301401 });
  v.settled.push(call({ id: 16, raw_p: .549, stated_p: .488, outcome: 0, resolve_by: Date.parse('2026-09-22T21:59Z') / 1000,
    resolved_at: Date.parse('2026-09-24T12:30Z') / 1000 }));
  v.summary = { n: 2, brier: .173522, se: .064622, base_rate: .5, skill: .305912 };
  v.summary_gut = { n: 2, brier: .186881, se: .11452, base_rate: .5, skill: .252476 };
  v.by_domain = [{ domain: 'markets', n: 2, brier: .173522, brier_gut: .186881 }];
  v.bands.push({ lo: .4, hi: .6, n: 1, hits: 0, mean_p: .488, interval: [0, .7934567085261071] });
  v.bands_gut.push({ lo: .4, hi: .6, n: 1, hits: 0, mean_p: .549, interval: [0, .7934567085261071] });
  v.text = v.text.replace('1 resolved', '2 resolved').replace('Brier 0.109', 'Brier 0.174');
  mockGet.mockResolvedValue(v);
  const r = await render(<RecordScreen />); await flush(); expect(screen.getByTestId('record-months')).toBeTruthy();
  await fireEvent.press(screen.getByLabelText(`Open forecast F17: ${call().claim}`)); expect(screen.getByTestId('call-sheet')).toBeTruthy();
  mockApp.cfg.baseUrl = 'https://other-record.example'; await r.rerender(<RecordScreen />); await flush();
  expect(screen.queryByTestId('call-sheet')).toBeNull();
});
