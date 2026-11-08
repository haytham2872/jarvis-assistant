import { act, fireEvent, render, screen } from '@testing-library/react-native';

import { CommandSheet } from './CommandSheet';

const mockGet = jest.fn();
jest.mock('./client', () => ({ getCommands: (...a: unknown[]) => mockGet(...a) }));

const cfg = { baseUrl: 'https://j.example', token: 't' };
const commands = [
  { name: '/pace', usage: '/pace [off|low|normal|high]', description: "show or set the research loop's pace", choices: ['off', 'low', 'normal', 'high'] },
  { name: '/about', usage: '/about', description: 'what JARVIS is', choices: [] },
];

async function open() {
  const onRun = jest.fn();
  await render(<CommandSheet visible cfg={cfg} onClose={jest.fn()} onRun={onRun} />);
  await act(async () => {});
  return onRun;
}

beforeEach(() => { jest.clearAllMocks(); mockGet.mockResolvedValue(commands); });

it('lists every command with its one-line description', async () => {
  await open();
  expect(screen.getByText('/pace')).toBeTruthy();
  expect(screen.getByText("show or set the research loop's pace")).toBeTruthy();
  expect(screen.getByText('/about')).toBeTruthy();
});

it('runs a command without choices when tapped', async () => {
  const onRun = await open();
  await act(async () => { await fireEvent.press(screen.getByLabelText('/about')); });
  expect(onRun).toHaveBeenCalledWith('/about');
});

it('shows choices as chips and runs "<name> <choice>"', async () => {
  const onRun = await open();
  await act(async () => { await fireEvent.press(screen.getByLabelText('/pace')); });
  expect(onRun).not.toHaveBeenCalled();
  await act(async () => { await fireEvent.press(screen.getByLabelText('low')); });
  expect(onRun).toHaveBeenCalledWith('/pace low');
});

it('shows an error line when the list cannot be loaded', async () => {
  mockGet.mockRejectedValue(new Error('offline'));
  await open();
  expect(screen.getByText(/Can't reach JARVIS/)).toBeTruthy();
});
