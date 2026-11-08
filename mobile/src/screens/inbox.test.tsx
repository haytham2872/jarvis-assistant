// The Inbox tab's screen (slice 4). Kept outside src/app: Expo Router bundles every file there as a route.
import { act, fireEvent, render, screen } from '@testing-library/react-native';

import type { InboxView, Notice } from '../chat/types';
import Inbox from '../app/(tabs)/inbox';
import { clearViewMemory } from '../core/useCachedView';

const mockMarkRead = jest.fn();
const mockApp = {
  cfg: { baseUrl: 'https://j', token: 't' } as { baseUrl: string; token: string } | null,
  notices: { pushToken: null, markRead: (...a: unknown[]) => mockMarkRead(...a), unread: 2, route: 'both' },
  speaker: { state: { source: null as string | null, phase: 'idle' }, toggle: jest.fn() },
};
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

const mockGet = jest.fn();
const mockVote = jest.fn();
jest.mock('../chat/client', () => {
  class HttpError extends Error {
    status: number;
    constructor(status: number, message: string) {
      super(message);
      this.status = status;
    }
  }
  return { HttpError, getInbox: (...a: unknown[]) => mockGet(...a), sendInboxFeedback: (...a: unknown[]) => mockVote(...a) };
});
const { HttpError } = jest.requireMock('../chat/client') as { HttpError: new (s: number, m: string) => Error };

const now = Date.now() / 1000;
const note = (over: Partial<Notice>): Notice => ({ id: 1, ts: now - 60, kind: 'brief', text: 'Good morning.', discord: true, read: false, ...over });
const view = (notices: Notice[], over: Partial<InboxView> = {}): InboxView =>
  ({ notices, unread: notices.filter((n) => !n.read).length, route: 'both', push: 'unknown', ...over });

beforeEach(() => {
  clearViewMemory();
  jest.clearAllMocks();
  mockApp.cfg = { baseUrl: 'https://j', token: 't' };
  mockApp.notices.route = 'both';
  mockApp.notices.unread = 2;
  mockApp.speaker.state = { source: null, phase: 'idle' };
});

it('lists JARVIS\'s messages under their kind and marks them seen', async () => {
  mockGet.mockResolvedValue(view([note({ id: 2, kind: 'reminder', text: 'Reminder: call the notary' }), note({ id: 1 })]));
  await render(<Inbox />);
  expect(await screen.findByText('Reminder: call the notary')).toBeTruthy();
  expect(screen.getByText('Morning brief')).toBeTruthy();
  expect(screen.getByText('Today')).toBeTruthy();
  expect(mockMarkRead).toHaveBeenCalledWith(2);
  expect(screen.getByText('Discord + phone')).toBeTruthy();
});

it('nothing is marked read when everything was already seen', async () => {
  mockGet.mockResolvedValue(view([note({ id: 1, read: true })]));
  await render(<Inbox />);
  await screen.findByText('Good morning.');
  expect(mockMarkRead).not.toHaveBeenCalled();
});

it('Ask about this opens the chat answering that message', async () => {
  mockGet.mockResolvedValue(view([note({ id: 9, kind: 'research', text: 'Camus\'s notebooks went to the BnF.' })]));
  await render(<Inbox />);
  await fireEvent.press(await screen.findByLabelText('Ask about this Research'));
  expect(mockNavigate).toHaveBeenCalledWith({ pathname: '/', params: expect.objectContaining({ reply: '9', replyText: 'Camus\'s notebooks went to the BnF.' }) });
});

it('a check-in offers Today, and a long message folds until opened', async () => {
  mockGet.mockResolvedValue(view([note({ id: 3, kind: 'checkin', text: 'Evening check-in.' }), note({ id: 2, text: 'x'.repeat(600) })]));
  await render(<Inbox />);
  await fireEvent.press(await screen.findByText('Open Today'));
  expect(mockNavigate).toHaveBeenCalledWith('/today');
  await fireEvent.press(screen.getByText('Show all'));
  expect(screen.getByText('Show less')).toBeTruthy();
});

it('a message Discord could not take says it is only here', async () => {
  mockGet.mockResolvedValue(view([note({ id: 4, kind: 'watch', text: 'NVDA closed below 180', discord: false })]));
  await render(<Inbox />);
  expect(await screen.findByText('Could not be sent to Discord; it is only here.')).toBeTruthy();
});

it('the filters appear once there is more than one kind, and keep only theirs', async () => {
  mockGet.mockResolvedValue(view([note({ id: 2, kind: 'reminder', text: 'Reminder: gym' }), note({ id: 1, text: 'Brief text' })]));
  await render(<Inbox />);
  await screen.findByText('Reminder: gym');
  await fireEvent.press(screen.getByText('Reminders'));
  expect(screen.queryByText('Brief text')).toBeNull();
  expect(screen.getByText('Reminder: gym')).toBeTruthy();
});

it('says so when empty, and when the server is older than the app', async () => {
  mockGet.mockResolvedValue(view([]));
  const { unmount } = await render(<Inbox />);
  expect(await screen.findByText('Nothing yet')).toBeTruthy();
  await unmount();
  mockGet.mockRejectedValue(new HttpError(404, 'Not Found'));
  await render(<Inbox />);
  expect(await screen.findByText(/deploy it to see the Inbox/)).toBeTruthy();
});

it('asks for nothing when not paired', async () => {
  mockApp.cfg = null;
  await render(<Inbox />);
  expect(screen.getByText('Pair with JARVIS in Settings first.')).toBeTruthy();
  await act(async () => {});
  expect(mockGet).not.toHaveBeenCalled();
});

it('the route line opens Settings', async () => {
  mockApp.notices.route = 'phone';
  mockGet.mockResolvedValue(view([]));
  await render(<Inbox />);
  await fireEvent.press(await screen.findByText('Phone only'));
  expect(mockPush).toHaveBeenCalledWith('/settings');
});

it('what was new when the tab opened stays marked new, though it is marked read at once', async () => {
  mockGet.mockResolvedValueOnce(view([note({ id: 2 }), note({ id: 1, read: true, text: 'Old one' })]));
  mockGet.mockResolvedValue(view([note({ id: 2, read: true }), note({ id: 1, read: true, text: 'Old one' })]));
  const { rerender } = await render(<Inbox />);
  await screen.findByText('Old one');
  expect(screen.getByTestId('new-2')).toBeTruthy();
  expect(screen.queryByTestId('new-1')).toBeNull();
  mockApp.notices.unread = 0;              // the badge cleared: the tab reads again
  await rerender(<Inbox />);
  await act(async () => {});
  expect(screen.getByTestId('new-2')).toBeTruthy();
});

it('any message can be read aloud, from its id', async () => {
  mockGet.mockResolvedValue(view([note({ id: 7, kind: 'research', text: 'A finding.' })]));
  await render(<Inbox />);
  await fireEvent.press(await screen.findByLabelText('Read this Research aloud'));
  expect(mockApp.speaker.toggle).toHaveBeenCalledWith('notice:7', 'A finding.');
});

it("today's brief can be played from the top; an older one cannot", async () => {
  mockGet.mockResolvedValue(view([note({ id: 5, kind: 'brief', text: 'Good morning.' })]));
  const { unmount } = await render(<Inbox />);
  await fireEvent.press(await screen.findByLabelText('Play the brief'));
  expect(mockApp.speaker.toggle).toHaveBeenCalledWith('notice:5', 'Good morning.');
  await unmount();
  mockGet.mockResolvedValue(view([note({ id: 4, kind: 'brief', ts: now - 3 * 86400, text: 'Old brief.' })]));
  await render(<Inbox />);
  await screen.findByText('Old brief.');
  expect(screen.queryByLabelText('Play the brief')).toBeNull();
});

it('while the brief plays, its button stops it', async () => {
  mockApp.speaker.state = { source: 'notice:5', phase: 'speaking' };
  mockGet.mockResolvedValue(view([note({ id: 5, kind: 'brief', text: 'Good morning.' })]));
  await render(<Inbox />);
  expect(await screen.findByLabelText('Stop the brief')).toBeTruthy();
  expect(screen.getByLabelText('Stop reading this Morning brief')).toBeTruthy();
});

it('a research finding can be marked useful; tapping the chosen thumb clears it', async () => {
  mockGet.mockResolvedValue(view([note({ id: 8, kind: 'research', text: 'A finding.', vote: null })]));
  mockVote.mockImplementation(async (_c: unknown, _id: number, vote: string) => ({ vote: vote === 'none' ? null : vote }));
  await render(<Inbox />);
  await fireEvent.press(await screen.findByLabelText('Useful'));
  expect(mockVote).toHaveBeenLastCalledWith(expect.anything(), 8, 'up');
  expect(screen.getByLabelText('Useful').props.accessibilityState).toEqual(expect.objectContaining({ selected: true }));
  await fireEvent.press(screen.getByLabelText('Useful'));
  expect(mockVote).toHaveBeenLastCalledWith(expect.anything(), 8, 'none');
  expect(screen.getByLabelText('Useful').props.accessibilityState).toEqual(expect.objectContaining({ selected: false }));
  await fireEvent.press(screen.getByLabelText('Not useful'));
  expect(mockVote).toHaveBeenLastCalledWith(expect.anything(), 8, 'down');
});

it('a failed vote is reverted with a short error line; other kinds have no thumbs', async () => {
  mockGet.mockResolvedValue(view([note({ id: 8, kind: 'research', text: 'A finding.', vote: 'down' }), note({ id: 7, text: 'Brief.' })]));
  mockVote.mockRejectedValue(new Error('offline'));
  await render(<Inbox />);
  await screen.findByText('Brief.');
  expect(screen.getAllByLabelText('Useful')).toHaveLength(1);
  await fireEvent.press(screen.getByLabelText('Useful'));
  expect(await screen.findByText('Could not save that vote.')).toBeTruthy();
  expect(screen.getByLabelText('Not useful').props.accessibilityState).toEqual(expect.objectContaining({ selected: true }));
  expect(screen.getByLabelText('Useful').props.accessibilityState).toEqual(expect.objectContaining({ selected: false }));
});
