// The Portfolio tab's screen. Kept outside src/app: Expo Router bundles every file there as a route.
import { act, fireEvent, render, screen, within } from '@testing-library/react-native';

import type { PortfolioView, Position, Thesis } from '../chat/types';
import Portfolio from '../app/(tabs)/portfolio';
import { clearViewMemory } from '../core/useCachedView';

const mockApp = { cfg: { baseUrl: 'https://j', token: 't' } as { baseUrl: string; token: string } | null };
const mockNavigate = jest.fn();
const mockPush = jest.fn();
jest.mock('../state/AppContext', () => ({ useApp: () => mockApp }));
jest.mock('expo-router', () => ({
  useFocusEffect: (fn: () => void | (() => void)) => jest.requireActual('react').useEffect(fn, [fn]),
  router: { navigate: (...a: unknown[]) => mockNavigate(...a), push: (...a: unknown[]) => mockPush(...a) },
}));
jest.mock('react-native-screens/experimental', () => {
  const { View } = jest.requireActual('react-native');
  return { SafeAreaView: View };
});
jest.mock('react-native-safe-area-context', () => ({ useSafeAreaInsets: () => ({ top: 59, bottom: 34, left: 0, right: 0 }) }));
jest.mock('expo-haptics', () => ({ notificationAsync: jest.fn(), NotificationFeedbackType: { Success: 'success' } }));

const mockGet = jest.fn();
const mockHistory = jest.fn();
const mockDecide = jest.fn();
jest.mock('../chat/client', () => {
  class HttpError extends Error {
    status: number;
    constructor(status: number, message: string) {
      super(message);
      this.status = status;
    }
  }
  return { HttpError, getPortfolio: (...a: unknown[]) => mockGet(...a), decideApproval: (...a: unknown[]) => mockDecide(...a),
    getPortfolioHistory: (...a: unknown[]) => mockHistory(...a) };
});
const { HttpError } = jest.requireMock('../chat/client') as { HttpError: new (s: number, m: string) => Error };
const CFG = { baseUrl: 'https://j', token: 't' };

const nvda = (over: Partial<Position> = {}): Position => ({
  symbol: 'NVDA', name: 'NVIDIA', currency: 'USD', exchange: 'NASDAQ', shares: '1.14339682', average_cost: '152.1',
  cost_basis: '173.91', close: '165.37', close_date: '2026-10-14', close_source: 'Yahoo', close_url: 'https://y',
  prev_close: '166.55', day_change_pct: '-0.71', value: '189.08', pnl: '15.17', pnl_pct: '8.72',
  value_base: '168.26543523661366', pnl_base: '13.2', weight_pct: '20.00', realised: '0', held_for_split: false,
  closes: [['2026-10-13', '166.55'], ['2026-10-14', '165.37']],
  trades: [{ id: 1, side: 'opening', shares: '1.14339682', price: '152.1', currency: 'USD', traded_on: '2026-09-01' }],
  ...over,
});
const air = nvda({ symbol: 'AIR.PA', name: 'Airbus', currency: 'EUR', shares: '6', average_cost: '158.2', cost_basis: '949.2',
  close: '159.76', value: '958.56', pnl: '9.36', pnl_pct: '0.99', value_base: '958.56', pnl_base: '9.36', weight_pct: '80.00',
  day_change_pct: '1.07', trades: [] });
const thesis = (over: Partial<Thesis> = {}): Thesis => ({
  id: 7, symbol: 'NVDA', why: 'data centre demand keeps compounding', source: 'owner', horizon: 'two years',
  status: 'open', since: '2026-09-01', note: '',
  conditions: [{ id: 3, kind: 'price_below', direction: 'wrong_if', level: '140', effective_level: null,
    criterion: 'NVDA closes below 140', due_date: '', state: 'open', distance_pct: '15.34', linked: 'watch' }],
  last_event: { kind: 'condition_added', reason: 'NVDA closes below 140', date: '2026-09-02' }, ...over,
});

const view = (over: Partial<PortfolioView> = {}): PortfolioView => ({
  as_of: 1, positions: [air, nvda()], watching: [], closed: [], theses: [thesis()], splits: [],
  history: [{ date: '2026-10-13', value_base: '1120', profit_base: '20' }, { date: '2026-10-14', value_base: '1126.82', profit_base: '22.56' }],
  total: { value_base: '1126.82', pnl_base: '22.56', close_date: '2026-10-14', complete: true, note: '' },
  day: { date: '2026-10-14', change_base: '6.82', change_pct: '0.61' }, ...over,
});

beforeEach(() => {
  clearViewMemory();
  jest.clearAllMocks();
  mockApp.cfg = { ...CFG };
  mockGet.mockResolvedValue(view());
  mockHistory.mockResolvedValue({ range: '6M', currency: 'EUR', gaps: 0, stale: false, points: [
    { day: '2026-10-12', value_base: '1100', cost_base: '1104.26' }, { day: '2026-10-13', value_base: '1120', cost_base: '1104.26' },
    { day: '2026-10-14', value_base: '1126.82', cost_base: '1104.26' }] });
});

it('leads with the total, its P&L, the day and its value over a range against its cost', async () => {
  await render(<Portfolio />);
  const hero = within(await screen.findByTestId('hero'));
  expect(hero.getByText('€1,126.82')).toBeTruthy();
  expect(hero.getByText('+€22.56 (+2.04%)')).toBeTruthy();                   // 22.56 / 1,104.26
  expect(hero.getByText('+0.61% on the day')).toBeTruthy();
  expect(hero.getByText('Tracked total · close 14 Oct')).toBeTruthy();
  const chart = await screen.findByTestId('history-chart');
  expect(chart.props.accessibilityLabel).toBe('Total value over 6M, against what it cost');
  // no Skia in tests: the column chart, one bar per day
  expect(within(chart).getByTestId('chart-fallback', { includeHiddenElements: true }).children).toHaveLength(3);
  expect(mockHistory).toHaveBeenCalledWith(CFG, '6M', expect.anything());
  await act(async () => { await fireEvent.press(hero.getByLabelText('Total over 1Y')); });
  expect(mockHistory).toHaveBeenLastCalledWith(CFG, '1Y', expect.anything());
  expect(screen.getByLabelText('Allocation: AIR.PA 80.00%, NVDA 20.00%')).toBeTruthy();
});

it('a range the server cannot draw says why, and an older server without history still shows the total', async () => {
  mockHistory.mockResolvedValue({ range: '6M', currency: 'EUR', gaps: 0, stale: false, points: [],
    note: 'The price history could not be fetched just now.' });
  await render(<Portfolio />);
  expect(await screen.findByText('The price history could not be fetched just now.')).toBeTruthy();
  await screen.unmount();
  mockHistory.mockRejectedValue(new HttpError(404, 'Not Found'));
  await render(<Portfolio />);
  expect(within(await screen.findByTestId('hero')).getByText('€1,126.82')).toBeTruthy();
  expect(screen.queryByTestId('history-chart')).toBeNull();
});

it('shows each holding with its exact shares, value and P&L, and a tap opens its screen', async () => {
  await render(<Portfolio />);
  const card = within(await screen.findByTestId('position-NVDA'));
  expect(card.getByText('1.14339682 sh · avg $152.10')).toBeTruthy();
  expect(card.getByText('$189.08')).toBeTruthy();
  expect(card.getByText('+$15.17 · +8.72%')).toBeTruthy();
  expect(card.getByText('−0.71% on the day')).toBeTruthy();
  expect(within(card.getByTestId('chart-NVDA')).getByTestId('chart-fallback', { includeHiddenElements: true }).children).toHaveLength(2);
  await fireEvent.press(card.getByLabelText('NVDA, $189.08, +8.72%'));
  expect(mockPush).toHaveBeenCalledWith({ pathname: '/holding/[symbol]', params: { symbol: 'NVDA' } });
  expect(screen.queryByTestId('details-NVDA')).toBeNull();
});

it('flags a condition near or reached even while the card is closed', async () => {
  const near = thesis({ conditions: [{ ...thesis().conditions[0], distance_pct: '2.1' }] });
  mockGet.mockResolvedValue(view({ theses: [near] }));
  await render(<Portfolio />);
  expect(within(await screen.findByTestId('position-NVDA')).getByText('Wrong if it closes below $140.00: 2.1% away')).toBeTruthy();
  await screen.unmount();
  mockGet.mockResolvedValue(view());                                            // 15% away: nothing to flag
  await render(<Portfolio />);
  expect(within(await screen.findByTestId('position-NVDA')).queryByText(/away/)).toBeNull();
});

it("today's biggest movers, largest first, each opening its stock", async () => {
  await render(<Portfolio />);
  const movers = within(await screen.findByTestId('movers'));
  expect(movers.getAllByRole('button').map((b) => b.props.accessibilityLabel)).toEqual(['AIR.PA +1.07% on the day', 'NVDA −0.71% on the day']);
  await fireEvent.press(movers.getByLabelText('AIR.PA +1.07% on the day'));
  expect(mockPush).toHaveBeenCalledWith({ pathname: '/holding/[symbol]', params: { symbol: 'AIR.PA' } });
});

it('a split waiting for his yes is approved on its card, and the view read again', async () => {
  const split = { symbol: 'NVDA', split_date: '2026-10-14', ratio: '3-for-1', shares_before: '5', shares_after: '15',
    source: 'Yahoo', approval: { id: 9, expires_at: 2e9 } };
  mockGet.mockResolvedValue(view({ splits: [split], positions: [air, nvda({ held_for_split: true })],
    total: { value_base: null, pnl_base: null, close_date: null, complete: false, note: 'A split is waiting for your yes.' } }));
  mockDecide.mockResolvedValue({ text: "Approved #9. Recorded NVDA's split.\nPortfolio ledger (tracked positions):\nNVDA: 15 shares",
    approval: null });
  await render(<Portfolio />);
  const card = within(await screen.findByTestId('split-NVDA'));
  expect(card.getByText(/5 shares would become 15, same total cost/)).toBeTruthy();
  expect(within(screen.getByTestId('hero')).getByText('A split is waiting for your yes.')).toBeTruthy();
  expect(within(screen.getByTestId('hero')).getByText('Last complete total: €1,126.82 (14 Oct)')).toBeTruthy();
  expect(within(screen.getByTestId('position-NVDA')).getByText('Held for a split')).toBeTruthy();
  mockGet.mockResolvedValue(view());
  await act(async () => { await fireEvent.press(card.getByLabelText('Approve the NVDA split')); });
  expect(mockDecide).toHaveBeenCalledWith(CFG, 9, true);
  expect(screen.getByText("Approved #9. Recorded NVDA's split.")).toBeTruthy();
  expect(screen.queryByText(/Portfolio ledger/)).toBeNull();                       // that listing is for JARVIS
  expect(screen.queryByTestId('split-NVDA')).toBeNull();
  expect(mockGet).toHaveBeenCalledTimes(2);
});

it('a split with no live request points at JARVIS, and a wrong one can be dismissed through him', async () => {
  const split = { symbol: 'NVDA', split_date: '2026-10-14', ratio: '3-for-1', shares_before: '5', shares_after: '15',
    source: 'Yahoo', approval: null };
  mockGet.mockResolvedValue(view({ splits: [split] }));
  await render(<Portfolio />);
  const card = within(await screen.findByTestId('split-NVDA'));
  expect(card.queryByLabelText('Approve the NVDA split')).toBeNull();
  expect(card.getByText('JARVIS will ask you to confirm it.')).toBeTruthy();
  await fireEvent.press(card.getByText('Wrong split? Tell JARVIS to dismiss it'));
  expect(mockNavigate).toHaveBeenCalledWith({ pathname: '/', params: { draft: 'The NVDA split the feed reports is wrong, dismiss it.' } });
});

it('lists stocks he only watches with their thesis, and positions he sold', async () => {
  mockGet.mockResolvedValue(view({
    watching: [{ symbol: 'AMD', name: 'Advanced Micro Devices', currency: 'USD', close: '150.03', close_date: '2026-10-14' }],
    closed: [{ symbol: 'TSLA', name: 'Tesla', currency: 'USD', realised: '120' }],
    theses: [thesis(), thesis({ id: 8, symbol: 'AMD', why: 'MI400 wins share', source: 'my analysis', horizon: '', conditions: [] })],
  }));
  await render(<Portfolio />);
  const watch = within(await screen.findByTestId('watch-AMD'));
  expect(watch.getByText('$150.03')).toBeTruthy();
  expect(watch.getByText('My analysis')).toBeTruthy();
  expect(watch.getByText('MI400 wins share')).toBeTruthy();
  expect(screen.getByText('+$120.00 realised')).toBeTruthy();
  await fireEvent.press(watch.getByLabelText('AMD, watching, $150.03'));
  expect(mockPush).toHaveBeenLastCalledWith({ pathname: '/holding/[symbol]', params: { symbol: 'AMD' } });
});

it('says what is missing: an empty ledger, an older server, no pairing', async () => {
  mockGet.mockResolvedValue(view({ positions: [], theses: [], history: [], total: null, day: null }));
  await render(<Portfolio />);
  expect(await screen.findByText(/Nothing in the ledger yet/)).toBeTruthy();
  expect(screen.queryByTestId('hero')).toBeNull();
  await screen.unmount();
  mockGet.mockRejectedValue(new HttpError(404, 'Not Found'));
  await render(<Portfolio />);
  expect(await screen.findByText(/doesn't have the Portfolio view yet/)).toBeTruthy();
  await screen.unmount();
  mockGet.mockResolvedValue({ ...view({ positions: [] }), error: 'The portfolio could not be read just now.' });
  await render(<Portfolio />);
  expect(await screen.findByText('The portfolio could not be read just now.')).toBeTruthy();
  expect(screen.queryByText(/Nothing in the ledger yet/)).toBeNull();
  await screen.unmount();
  mockApp.cfg = null;
  await render(<Portfolio />);
  expect(screen.getByText('Pair with JARVIS in Settings first.')).toBeTruthy();
});
