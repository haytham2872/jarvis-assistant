// A holding's screen (next plan, slice 3). Kept outside src/app: Expo Router bundles every file
// there as a route. No Skia in tests, so the chart is the column fallback; the drag that sets a
// level is covered by LineChart.test.tsx, and the button below the chart opens the same sheet.
import { act, fireEvent, render, screen, within } from '@testing-library/react-native';

import type { HoldingView, Position, Thesis } from '../chat/types';
import Holding from '../app/holding/[symbol]';
import { clearViewMemory } from '../core/useCachedView';

const mockApp = { cfg: { baseUrl: 'https://j', token: 't' } as { baseUrl: string; token: string } | null };
const mockNavigate = jest.fn();
let mockSymbol = 'NVDA';
jest.mock('../state/AppContext', () => ({ useApp: () => mockApp }));
jest.mock('expo-router', () => ({
  useFocusEffect: (fn: () => void | (() => void)) => jest.requireActual('react').useEffect(fn, [fn]),
  useLocalSearchParams: () => ({ symbol: mockSymbol }),
  router: { navigate: (...a: unknown[]) => mockNavigate(...a), back: jest.fn(), canGoBack: () => true, replace: jest.fn() },
}));
jest.mock('react-native-safe-area-context', () => ({ useSafeAreaInsets: () => ({ top: 59, bottom: 34, left: 0, right: 0 }) }));
jest.mock('expo-haptics', () => ({
  notificationAsync: jest.fn(), selectionAsync: jest.fn(), impactAsync: jest.fn(),
  NotificationFeedbackType: { Success: 'success' }, ImpactFeedbackStyle: { Light: 'light' },
}));
jest.mock('expo-symbols', () => ({ SymbolView: ({ fallback }: { fallback: unknown }) => fallback ?? null }));

const mockGet = jest.fn();
const mockWatch = jest.fn();
const mockCondition = jest.fn();
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
    getHolding: (...a: unknown[]) => mockGet(...a),
    proposeWatch: (...a: unknown[]) => mockWatch(...a),
    proposeCondition: (...a: unknown[]) => mockCondition(...a),
  };
});
const { HttpError } = jest.requireMock('../chat/client') as { HttpError: new (s: number, m: string) => Error };
const CFG = { baseUrl: 'https://j', token: 't' };

const position: Position = {
  symbol: 'NVDA', name: 'NVIDIA', currency: 'USD', exchange: 'NASDAQ', shares: '1.14339682', average_cost: '152.1',
  cost_basis: '173.91', close: '165.37', close_date: '2026-10-14', close_source: 'Yahoo', close_url: 'https://y',
  prev_close: '166.55', day_change_pct: '-0.71', value: '189.08', pnl: '15.17', pnl_pct: '8.72',
  value_base: '168.26543523661366', pnl_base: '13.2', weight_pct: '20.00', realised: '0', held_for_split: false,
  closes: [['2026-10-13', '166.55'], ['2026-10-14', '165.37']],
  trades: [{ id: 1, side: 'opening', shares: '1.14339682', price: '152.1', currency: 'USD', traded_on: '2026-09-01' }],
};
const thesis: Thesis = {
  id: 7, symbol: 'NVDA', why: 'data centre demand keeps compounding', source: 'owner', horizon: 'two years',
  status: 'open', since: '2026-09-01', note: '',
  conditions: [{ id: 3, kind: 'price_below', direction: 'wrong_if', level: '140', effective_level: null,
    criterion: 'NVDA closes below 140', due_date: '', state: 'open', distance_pct: '15.34', linked: 'watch' }],
  last_event: { kind: 'condition_added', reason: 'NVDA closes below 140', date: '2026-09-02' },
};
const view = (over: Partial<HoldingView> = {}): HoldingView => ({
  symbol: 'NVDA', name: 'NVIDIA', currency: 'USD', range: '1Y', start: '2025-10-15', stale: false,
  history: [['2026-09-01', '150'], ['2026-09-02', '152'], ['2026-10-14', '165.37']], splits: [],
  trades: [{ id: 1, day: '2026-09-01', side: 'opening', shares: '1.14339682', price: '152.1', fees: '0' }],
  average_cost: '152.1', shares: '1.14339682',
  levels: [{ id: 3, thesis_id: 7, kind: 'price_below', direction: 'wrong_if', level: '140', set_level: '140', state: 'open',
    criterion: 'NVDA closes below 140', distance_pct: '15.34' }],
  dated: [], forecasts: [{ id: 4, claim: 'NVDA beats Q3 guidance', p: 0.62, state: 'open', outcome: null,
    made: '2026-09-02', due: '2026-11-20', settled: null }],
  position, watching: null, closed: null, theses: [thesis], split: null, ...over,
});

beforeEach(() => {
  clearViewMemory();
  jest.clearAllMocks();
  mockSymbol = 'NVDA';
  mockApp.cfg = { ...CFG };
  mockGet.mockResolvedValue(view());
});

it('reads the last close and the move over the range, with the ledger figures, thesis, forecasts and trades below', async () => {
  await render(<Holding />);
  expect(await screen.findByText('$165.37')).toBeTruthy();
  expect(screen.getByText('+10.25% over 1y')).toBeTruthy();                     // 150 to 165.37
  const chart = screen.getByTestId('price-chart');
  expect(chart.props.accessibilityLabel).toBe('NVDA price chart, 1Y, last $165.37');
  expect(within(chart).getByTestId('chart-fallback', { includeHiddenElements: true }).children).toHaveLength(3);
  expect(screen.getByText('€168.27 (+€13.20)')).toBeTruthy();
  expect(screen.getByText('Opening 1.14339682 at $152.10 · 1 Sep')).toBeTruthy();
  expect(screen.getByText('Your thesis · two years')).toBeTruthy();
  expect(screen.getByText('Wrong if it closes below $140.00')).toBeTruthy();
  expect(screen.getByText('15.3% away')).toBeTruthy();
  expect(screen.getByText('Condition added: NVDA closes below 140 (2 Sep)')).toBeTruthy();
  expect(screen.getByText('NVDA beats Q3 guidance · 62% · due 20 Nov')).toBeTruthy();
  expect(mockGet).toHaveBeenCalledWith(CFG, 'NVDA', '1Y', expect.anything());
});

it('a range chip reads that range', async () => {
  await render(<Holding />);
  await screen.findByText('$165.37');
  await act(async () => { await fireEvent.press(screen.getByLabelText('Show 6M')); });
  expect(mockGet).toHaveBeenLastCalledWith(CFG, 'NVDA', '6M', expect.anything());
  await act(async () => { await fireEvent.press(screen.getByLabelText('Show Since bought')); });
  expect(mockGet).toHaveBeenLastCalledWith(CFG, 'NVDA', 'ALL', expect.anything());
});

it("a stock he only watches has no 'since bought': its range chip says All", async () => {
  mockGet.mockResolvedValue(view({ trades: [], average_cost: null, shares: null, position: null }));
  await render(<Holding />);
  await screen.findByText('$165.37');
  expect(screen.queryByLabelText('Show Since bought')).toBeNull();
  await act(async () => { await fireEvent.press(screen.getByLabelText('Show All')); });
  expect(mockGet).toHaveBeenLastCalledWith(CFG, 'NVDA', 'ALL', expect.anything());
});

async function openSheet(price: string) {
  await render(<Holding />);
  await screen.findByText('$165.37');
  await fireEvent.press(screen.getByText('Add an alert at a price'));
  const sheet = within(screen.getByTestId('proposal-sheet'));
  await fireEvent.changeText(sheet.getByLabelText('Price in USD'), price);
  return sheet;
}

it('a price below the last close becomes a watch only when he taps Watch, and the screen reads again', async () => {
  mockWatch.mockResolvedValue({ text: "Watching: NVDA closes below 158.5 USD. Checked once a day; you'll get a message when it happens.", new: true });
  const sheet = await openSheet('158.5');
  expect(sheet.getByText('NVDA closing below $158.50?')).toBeTruthy();
  expect(sheet.getByText('Below the last close: it watches for a fall.')).toBeTruthy();
  expect(mockWatch).not.toHaveBeenCalled();
  await act(async () => { await fireEvent.press(sheet.getByLabelText('Watch NVDA closing below $158.50')); });
  expect(mockWatch).toHaveBeenCalledWith(CFG, 'NVDA', 'below', 158.5);
  expect(screen.queryByTestId('proposal-sheet')).toBeNull();
  expect(screen.getByText(/Watching: NVDA closes below 158.5 USD/)).toBeTruthy();
  expect(mockGet).toHaveBeenCalledTimes(2);
});

it('a price above it can go on the thesis as a "right if" condition', async () => {
  mockCondition.mockResolvedValue({ text: 'Added to the thesis: Right if NVDA closes above 250. Checked at every close.', new: true });
  const sheet = await openSheet('250');
  expect(sheet.getByText('NVDA closing above $250.00?')).toBeTruthy();
  expect(sheet.getByText('data centre demand keeps compounding')).toBeTruthy();       // the only open thesis, chosen
  await fireEvent.press(sheet.getByLabelText('Right if'));
  await act(async () => { await fireEvent.press(sheet.getByLabelText('Add to thesis')); });
  expect(mockCondition).toHaveBeenCalledWith(CFG, 'NVDA', 7, 'right_if', 'price_above', 250);
  expect(screen.getByText(/Added to the thesis: Right if NVDA closes above 250/)).toBeTruthy();
});

it("the server's refusal is shown on the sheet, which stays open; nothing is sent for a price that isn't one", async () => {
  mockWatch.mockRejectedValue(new HttpError(409, 'already 20 active watches -- cancel one first.'));
  const sheet = await openSheet('abc');
  expect(sheet.getByText('Type a price above zero.')).toBeTruthy();
  await fireEvent.press(sheet.getByLabelText('Watch'));
  expect(mockWatch).not.toHaveBeenCalled();
  await fireEvent.changeText(sheet.getByLabelText('Price in USD'), '120');
  await act(async () => { await fireEvent.press(sheet.getByLabelText('Watch NVDA closing below $120.00')); });
  expect(sheet.getByText('already 20 active watches -- cancel one first.')).toBeTruthy();
  expect(screen.getByTestId('proposal-sheet')).toBeTruthy();
});

it('without an open thesis the sheet offers only the watch; with two he picks one', async () => {
  mockGet.mockResolvedValue(view({ theses: [] }));
  const none = await openSheet('120');
  expect(none.queryByLabelText('Add to thesis')).toBeNull();
  await screen.unmount();
  mockGet.mockResolvedValue(view({ theses: [thesis, { ...thesis, id: 9, why: 'margins hold' }] }));
  const two = await openSheet('120');
  const add = two.getByLabelText('Add to thesis');
  expect(add.props.accessibilityState?.disabled).toBe(true);
  await fireEvent.press(two.getByLabelText('Thesis: margins hold'));
  expect(two.getByLabelText('Add to thesis').props.accessibilityState?.disabled).toBe(false);
});

it('Ask JARVIS types the question into the chat; a euro stock shows no euro conversion', async () => {
  await render(<Holding />);
  await fireEvent.press(await screen.findByLabelText('Ask JARVIS about NVDA'));
  expect(mockNavigate).toHaveBeenCalledWith({ pathname: '/', params: { draft: 'Is my NVDA thesis still holding?' } });
  await screen.unmount();
  mockSymbol = 'AIR.PA';
  mockGet.mockResolvedValue(view({ symbol: 'AIR.PA', name: 'Airbus', currency: 'EUR', theses: [], levels: [], forecasts: [],
    position: { ...position, symbol: 'AIR.PA', currency: 'EUR', trades: [] } }));
  await render(<Holding />);
  expect(await screen.findByText('€165.37')).toBeTruthy();
  expect(screen.queryByText('In euros')).toBeNull();
  await fireEvent.press(screen.getByLabelText('Ask JARVIS about AIR.PA'));
  expect(mockNavigate).toHaveBeenLastCalledWith({ pathname: '/', params: { draft: 'How is my AIR.PA position doing?' } });
});

it('says plainly when the stock is not his, when the feed is down, and when the history is old', async () => {
  mockGet.mockRejectedValue(new HttpError(404, 'That stock is not in your portfolio.'));
  await render(<Holding />);
  expect(await screen.findByText('That stock is not in your portfolio.')).toBeTruthy();
  await screen.unmount();
  mockGet.mockRejectedValue(new HttpError(503, 'The price history could not be fetched just now.'));
  await render(<Holding />);
  expect(await screen.findByText('The price history could not be fetched just now.')).toBeTruthy();
  await screen.unmount();
  mockGet.mockResolvedValue(view({ stale: true }));
  await render(<Holding />);
  expect(await screen.findByText(/last history fetched/)).toBeTruthy();
});
