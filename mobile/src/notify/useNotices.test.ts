// The Inbox count, the delivery setting and the phone's own notifications (slice 4).
import { act, renderHook, waitFor } from '@testing-library/react-native';

import type { InboxView, Notice, UpcomingItem } from '../chat/types';
import { useNotices } from './useNotices';

jest.mock('expo-notifications', () => ({
  setNotificationHandler: jest.fn(),
  getPermissionsAsync: jest.fn(),
  requestPermissionsAsync: jest.fn(),
  cancelAllScheduledNotificationsAsync: jest.fn(),
  scheduleNotificationAsync: jest.fn(),
  getExpoPushTokenAsync: jest.fn(),
  addNotificationReceivedListener: jest.fn(),
  addNotificationResponseReceivedListener: jest.fn(),
  getLastNotificationResponseAsync: jest.fn(),
  SchedulableTriggerInputTypes: { DATE: 'date' },
}));
const mockN = jest.requireMock('expo-notifications') as Record<string, jest.Mock>;
const mockStore: Record<string, string> = {};
jest.mock('expo-secure-store', () => ({
  getItemAsync: async (k: string) => mockStore[k] ?? null,
  setItemAsync: async (k: string, v: string) => { mockStore[k] = v; },
}));
jest.mock('expo-device', () => ({ isDevice: true }));
jest.mock('expo-constants', () => ({ expoConfig: { extra: { eas: { projectId: 'proj-1' } } } }));
const mockNavigate = jest.fn();
jest.mock('expo-router', () => ({ router: { navigate: (...a: unknown[]) => mockNavigate(...a) } }));

const mockApi = {
  getInbox: jest.fn(), getUpcoming: jest.fn(), markInboxRead: jest.fn(), setNoticeRoute: jest.fn(),
  registerPushToken: jest.fn(), confirmPushToken: jest.fn(),
};
jest.mock('../chat/client', () => ({
  getInbox: (...a: unknown[]) => mockApi.getInbox(...a),
  getUpcoming: (...a: unknown[]) => mockApi.getUpcoming(...a),
  markInboxRead: (...a: unknown[]) => mockApi.markInboxRead(...a),
  setNoticeRoute: (...a: unknown[]) => mockApi.setNoticeRoute(...a),
  registerPushToken: (...a: unknown[]) => mockApi.registerPushToken(...a),
  confirmPushToken: (...a: unknown[]) => mockApi.confirmPushToken(...a),
}));
const api = mockApi;

const cfg = { baseUrl: 'https://j', token: 't' };
const inbox = (over: Partial<InboxView> = {}): InboxView => ({ notices: [], unread: 0, route: 'discord', push: 'unknown', ...over });
const soon = (id: string, inS: number): UpcomingItem =>
  ({ id, kind: 'reminder', at: Date.now() / 1000 + inS, title: 'Reminder', body: id, screen: 'inbox' });
let received: ((n: unknown) => void) | null = null;
let tapped: ((r: unknown) => void) | null = null;

beforeEach(() => {
  jest.clearAllMocks();
  for (const k of Object.keys(mockStore)) delete mockStore[k];
  received = null;
  tapped = null;
  mockN.getPermissionsAsync.mockResolvedValue({ status: 'granted' });
  mockN.requestPermissionsAsync.mockResolvedValue({ status: 'granted' });
  mockN.getLastNotificationResponseAsync.mockResolvedValue(null);
  mockN.addNotificationReceivedListener.mockImplementation((f) => { received = f; return { remove: jest.fn() }; });
  mockN.addNotificationResponseReceivedListener.mockImplementation((f) => { tapped = f; return { remove: jest.fn() }; });
  api.getUpcoming.mockResolvedValue({ items: [soon('gym', 3600)], route: 'phone', push: 'unknown' });
});

const data = (d: Record<string, unknown>) => ({ request: { content: { data: d } } });

it('reads the unread count and schedules nothing while messages go to Discord only', async () => {
  api.getInbox.mockResolvedValue(inbox({ unread: 3 }));
  mockN.getPermissionsAsync.mockResolvedValue({ status: 'undetermined' });   // would be asked, if it asked
  const h = await renderHook(() => useNotices(cfg, null));
  await waitFor(() => expect(h.result.current.unread).toBe(3));
  expect(api.getUpcoming).not.toHaveBeenCalled();
  expect(mockN.scheduleNotificationAsync).not.toHaveBeenCalled();
  expect(mockN.requestPermissionsAsync).not.toHaveBeenCalled();     // never asks unless the phone is told
});

it('with the phone told, schedules the coming notifications, and leaves them alone when unchanged', async () => {
  api.getInbox.mockResolvedValue(inbox({ route: 'phone' }));
  const h = await renderHook(() => useNotices(cfg, null));
  await waitFor(() => expect(h.result.current.scheduled).toBe(1));
  const call = mockN.scheduleNotificationAsync.mock.calls[0][0];
  expect(call.content).toMatchObject({ title: 'Reminder', body: 'gym', data: { screen: 'inbox', kind: 'reminder' } });
  expect(call.trigger.type).toBe('date');
  await act(async () => { await h.result.current.refresh(); });
  expect(mockN.cancelAllScheduledNotificationsAsync).toHaveBeenCalledTimes(1);
  expect(mockN.scheduleNotificationAsync).toHaveBeenCalledTimes(1);
});

it('notifications turned off for Expo Go schedule nothing', async () => {
  api.getInbox.mockResolvedValue(inbox({ route: 'both' }));
  mockN.getPermissionsAsync.mockResolvedValue({ status: 'denied' });
  mockN.requestPermissionsAsync.mockResolvedValue({ status: 'denied' });
  const h = await renderHook(() => useNotices(cfg, null));
  await waitFor(() => expect(h.result.current.permission).toBe('denied'));
  expect(mockN.scheduleNotificationAsync).not.toHaveBeenCalled();
});

it('choosing the phone saves it, registers for pushes, and the test notification confirms them', async () => {
  // The server's view of this phone's token, as GET /inbox answers it with X-Push-Token.
  let server: 'unknown' | 'waiting' | 'confirmed' = 'unknown';
  api.getInbox.mockImplementation(async (_c: unknown, token: string | null) =>
    inbox({ route: server === 'unknown' ? 'discord' : 'phone', push: token ? server : 'unknown' }));
  api.setNoticeRoute.mockResolvedValue({ route: 'phone', push: 'unknown' });
  mockN.getExpoPushTokenAsync.mockResolvedValue({ data: 'ExponentPushToken[x]' });
  api.registerPushToken.mockImplementation(async () => { server = 'waiting'; return { route: 'phone', push: 'waiting', test_sent: true }; });
  api.confirmPushToken.mockImplementation(async () => { server = 'confirmed'; return { route: 'phone', push: 'confirmed' }; });
  const h = await renderHook(() => useNotices(cfg, null));
  await waitFor(() => expect(api.getInbox).toHaveBeenCalled());
  await act(async () => { expect(await h.result.current.setRoute('phone')).toBe(''); });
  expect(api.setNoticeRoute).toHaveBeenCalledWith(cfg, 'phone', null);
  expect(mockN.getExpoPushTokenAsync).toHaveBeenCalledWith({ projectId: 'proj-1' });
  expect(api.registerPushToken).toHaveBeenCalledWith(cfg, 'ExponentPushToken[x]');
  expect(h.result.current.push).toBe('waiting');
  await act(async () => { received!(data({ test: true, token: 'ExponentPushToken[x]' })); });
  await waitFor(() => expect(h.result.current.push).toBe('confirmed'));
  expect(api.confirmPushToken).toHaveBeenCalledWith(cfg, 'ExponentPushToken[x]');
});

it('a push token Expo will not give is reported, and scheduling still works', async () => {
  api.getInbox.mockResolvedValue(inbox({ route: 'phone' }));
  api.setNoticeRoute.mockResolvedValue({ route: 'phone', push: 'unknown' });
  mockN.getExpoPushTokenAsync.mockRejectedValue(new Error('Expo Go cannot do that'));
  const h = await renderHook(() => useNotices(cfg, null));
  await act(async () => { await h.result.current.setRoute('phone'); });
  expect(h.result.current.push).toBe('unavailable');
  expect(h.result.current.pushReason).toMatch(/Expo Go cannot/);
  await waitFor(() => expect(h.result.current.scheduled).toBe(1));
});

it('a tapped notification opens its screen', async () => {
  api.getInbox.mockResolvedValue(inbox());
  await renderHook(() => useNotices(cfg, null));
  await waitFor(() => expect(tapped).not.toBeNull());
  await act(() => tapped!({ notification: data({ screen: 'today', kind: 'checkin' }) }));
  expect(mockNavigate).toHaveBeenCalledWith('/today');
});

it('a message announced live reads the count again', async () => {
  api.getInbox.mockResolvedValue(inbox({ unread: 0 }));
  const h = await renderHook(({ n }: { n: Notice | null }) => useNotices(cfg, n), { initialProps: { n: null as Notice | null } });
  await waitFor(() => expect(api.getInbox).toHaveBeenCalledTimes(1));
  api.getInbox.mockResolvedValue(inbox({ unread: 1 }));
  await h.rerender({ n: { id: 5, ts: 1, kind: 'watch', text: 'x', discord: true, read: false } });
  await waitFor(() => expect(h.result.current.unread).toBe(1));
});

it('marking read takes the count the server answers', async () => {
  api.getInbox.mockResolvedValue(inbox({ unread: 4 }));
  api.markInboxRead.mockResolvedValue({ unread: 0 });
  const h = await renderHook(() => useNotices(cfg, null));
  await waitFor(() => expect(h.result.current.unread).toBe(4));
  await act(async () => { await h.result.current.markRead(9); });
  expect(api.markInboxRead).toHaveBeenCalledWith(cfg, 9);
  expect(h.result.current.unread).toBe(0);
});

it('a notification carrying a token but not the test does not confirm pushes', async () => {
  api.getInbox.mockResolvedValue(inbox());
  await renderHook(() => useNotices(cfg, null));
  await waitFor(() => expect(received).not.toBeNull());
  await act(async () => { received!(data({ token: 'ExponentPushToken[x]', kind: 'brief' })); });
  expect(api.confirmPushToken).not.toHaveBeenCalled();
});

it('pushes confirmed earlier are read on opening, and then nothing is scheduled here', async () => {
  mockStore['jarvis.push.v1'] = 'ExponentPushToken[x]';
  api.getInbox.mockImplementation(async (_c: unknown, token: string | null) =>
    inbox({ route: 'phone', push: token ? 'confirmed' : 'unknown' }));
  const h = await renderHook(() => useNotices(cfg, null));
  await waitFor(() => expect(h.result.current.push).toBe('confirmed'));
  expect(h.result.current.pushToken).toBe('ExponentPushToken[x]');
  expect(mockN.scheduleNotificationAsync).not.toHaveBeenCalled();
});
