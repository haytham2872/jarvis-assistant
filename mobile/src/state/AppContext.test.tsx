import { act, fireEvent, render, screen } from '@testing-library/react-native';
import { Pressable, Text } from 'react-native';

import { AppProvider, useApp } from './AppContext';

// The notifications library's native module is a bare stub under Jest; since SDK 58 its
// setNotificationHandler calls remove() on the stub's subscription, which has none.
jest.mock('expo-notifications', () => ({
  setNotificationHandler: jest.fn(),
  getPermissionsAsync: jest.fn(async () => ({ status: 'undetermined' })),
  requestPermissionsAsync: jest.fn(async () => ({ status: 'undetermined' })),
  cancelAllScheduledNotificationsAsync: jest.fn(async () => undefined),
  scheduleNotificationAsync: jest.fn(async () => 'n1'),
  getExpoPushTokenAsync: jest.fn(async () => ({ data: 'ExponentPushToken[test]' })),
  addNotificationReceivedListener: jest.fn(() => ({ remove: jest.fn() })),
  addNotificationResponseReceivedListener: jest.fn(() => ({ remove: jest.fn() })),
  getLastNotificationResponseAsync: jest.fn(async () => null),
  SchedulableTriggerInputTypes: { DATE: 'date' },
}));

const mockSettings = { baseUrl: '', token: '' };
jest.mock('../core/settings', () => ({ loadSettings: async () => mockSettings }));
jest.mock('../voice/prefs', () => ({
  DEFAULT_PREFS: { speakVoiceReplies: true, rate: 1 }, loadPrefs: async () => ({ speakVoiceReplies: false, rate: 1.25 }),
}));
jest.mock('../voice/useSpeaker', () => ({ useSpeaker: () => ({ state: { source: null, phase: 'idle' }, speak: jest.fn() }) }));
const mockApprovals: { state: string }[] = [];
jest.mock('../chat/useChat', () => ({ useChat: () => ({ state: { approvals: {} } }) }));
jest.mock('../chat/reducer', () => ({ waitingApprovals: () => mockApprovals }));

function Probe() {
  const app = useApp();
  return <Text>{`${app.checked}|${app.cfg ? app.cfg.baseUrl : 'none'}|${app.prefs.rate}|${app.waitingCount}`}</Text>;
}

it('reads the settings and preferences once, and stays unpaired without a server', async () => {
  mockSettings.baseUrl = '';
  await render(<AppProvider><Probe /></AppProvider>);
  expect(await screen.findByText('true|none|1.25|0')).toBeTruthy();
});

it('is paired with a server and token, and counts only approvals still waiting', async () => {
  mockSettings.baseUrl = 'https://j';
  mockSettings.token = 't';
  mockApprovals.splice(0, mockApprovals.length, { state: 'pending' }, { state: 'executing' }, { state: 'pending' });
  await render(<AppProvider><Probe /></AppProvider>);
  expect(await screen.findByText('true|https://j|1.25|2')).toBeTruthy();
});

it('a screen outside the provider is a programming error', async () => {
  jest.spyOn(console, 'error').mockImplementation(() => undefined);
  await expect(render(<Probe />)).rejects.toThrow(/outside AppProvider/);
  await act(() => undefined);
});

it('reloading keeps the same connection while the settings are the same, and drops it once unpaired', async () => {
  mockSettings.baseUrl = 'https://j';
  mockSettings.token = 't';
  const seen: unknown[] = [];
  function Reloader() {
    const app = useApp();
    seen.push(app.cfg);
    return <Pressable onPress={() => void app.reload()}><Text>{app.cfg ? 'paired' : 'unpaired'}</Text></Pressable>;
  }
  await render(<AppProvider><Reloader /></AppProvider>);
  await screen.findByText('paired');
  const first = seen[seen.length - 1];
  await act(async () => { await fireEvent.press(screen.getByText('paired')); });
  expect(seen[seen.length - 1]).toBe(first);           // same object: nothing re-subscribes
  mockSettings.baseUrl = '';
  await act(async () => { await fireEvent.press(screen.getByText('paired')); });
  expect(screen.getByText('unpaired')).toBeTruthy();
});
