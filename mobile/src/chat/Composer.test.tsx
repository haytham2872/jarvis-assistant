import { act, fireEvent, render, screen } from '@testing-library/react-native';

import { Composer, elapsed } from './Composer';

const mockRecorder = {
  start: jest.fn(), finish: jest.fn(), cancel: jest.fn(), active: false, ms: 0, metering: -30 as number | undefined,
};
jest.mock('../voice/useRecorder', () => ({ useRecorder: () => mockRecorder }));

async function setup(draft = '') {
  const props = {
    enabled: true, draft, setDraft: jest.fn(), onSendText: jest.fn(),
    onRecordStart: jest.fn(), onRecording: jest.fn().mockResolvedValue(null),
  };
  const view = await render(<Composer {...props} />);
  return { props, view };
}

beforeEach(() => {
  jest.clearAllMocks();
  mockRecorder.active = false;
  mockRecorder.ms = 0;
});

it('offers the mic when there is nothing typed, and Send when there is', async () => {
  await setup();
  expect(screen.getByLabelText('Record a voice message')).toBeTruthy();
  expect(screen.queryByLabelText('Send')).toBeNull();
  await screen.unmount();
  await setup('hello');
  expect(screen.getByLabelText('Send')).toBeTruthy();
});

it('warms the transcriber as recording starts', async () => {
  mockRecorder.start.mockResolvedValue(null);
  const { props } = await setup();
  await act(async () => { await fireEvent.press(screen.getByLabelText('Record a voice message')); });
  expect(props.onRecordStart).toHaveBeenCalled();
  expect(mockRecorder.start).toHaveBeenCalled();
});

it('says how to turn the microphone back on when permission is off', async () => {
  mockRecorder.start.mockResolvedValue('permission');
  await setup();
  await act(async () => { await fireEvent.press(screen.getByLabelText('Record a voice message')); });
  expect(screen.getByText('The microphone is off for Expo Go.')).toBeTruthy();
  expect(screen.getByText('Open Settings')).toBeTruthy();
});

it('while recording: time, cancel and send; sending hands over the file', async () => {
  mockRecorder.active = true;
  mockRecorder.ms = 65_400;
  mockRecorder.finish.mockResolvedValue({ uri: 'file:///r.m4a', ms: 65_400, bytes: 900_000 });
  const { props } = await setup();
  expect(screen.getByText('1:05')).toBeTruthy();
  expect(screen.getByLabelText('Cancel recording')).toBeTruthy();
  await act(async () => { await fireEvent.press(screen.getByLabelText('Send recording')); });
  expect(props.onRecording).toHaveBeenCalledWith('file:///r.m4a', 65_400);
});

it('shows what the server said when nothing was heard', async () => {
  mockRecorder.active = true;
  mockRecorder.finish.mockResolvedValue({ uri: 'file:///r.m4a', ms: 3_000, bytes: 40_000 });
  const { props } = await setup();
  props.onRecording.mockResolvedValue('I heard nothing in that recording');
  await act(async () => { await fireEvent.press(screen.getByLabelText('Send recording')); });
  expect(screen.getByText('I heard nothing in that recording')).toBeTruthy();
});

it('cancel throws the recording away without sending it', async () => {
  mockRecorder.active = true;
  const { props } = await setup();
  await act(async () => { await fireEvent.press(screen.getByLabelText('Cancel recording')); });
  expect(mockRecorder.cancel).toHaveBeenCalled();
  expect(props.onRecording).not.toHaveBeenCalled();
});

it('formats elapsed time as m:ss', () => {
  expect(elapsed(0)).toBe('0:00');
  expect(elapsed(9_999)).toBe('0:09');
  expect(elapsed(120_000)).toBe('2:00');
});

it('feeds the orb the real microphone level while recording, and says when recording starts and stops', async () => {
  const push = jest.fn();
  const onActiveChange = jest.fn();
  const props = {
    enabled: true, draft: '', setDraft: jest.fn(), onSendText: jest.fn(),
    onRecordStart: jest.fn(), onRecording: jest.fn().mockResolvedValue(null),
    levels: { push, subscribe: () => () => undefined }, onActiveChange,
  };
  await render(<Composer {...props} />);
  expect(push).toHaveBeenLastCalledWith(0);
  expect(onActiveChange).toHaveBeenLastCalledWith(false);
  mockRecorder.active = true;
  mockRecorder.metering = -30;
  await screen.rerender(<Composer {...props} />);
  expect(push).toHaveBeenLastCalledWith(0.5);
  expect(onActiveChange).toHaveBeenLastCalledWith(true);
});

it('starts recording by itself when the /talk link opened the app, once', async () => {
  mockRecorder.start.mockResolvedValue(null);
  const onAutoStarted = jest.fn();
  const props = {
    enabled: true, draft: '', setDraft: jest.fn(), onSendText: jest.fn(),
    onRecordStart: jest.fn(), onRecording: jest.fn().mockResolvedValue(null), autoStart: false, onAutoStarted,
  };
  await render(<Composer {...props} />);
  expect(mockRecorder.start).not.toHaveBeenCalled();
  await act(async () => { await screen.rerender(<Composer {...props} autoStart />); });
  expect(mockRecorder.start).toHaveBeenCalledTimes(1);
  expect(onAutoStarted).toHaveBeenCalledTimes(1);
  await act(async () => { await screen.rerender(<Composer {...props} autoStart />); });
  expect(mockRecorder.start).toHaveBeenCalledTimes(1);
});

it('offers conversation mode beside the mic when nothing is typed, and only then', async () => {
  const onConversation = jest.fn();
  const props = {
    enabled: true, draft: '', setDraft: jest.fn(), onSendText: jest.fn(),
    onRecordStart: jest.fn(), onRecording: jest.fn().mockResolvedValue(null), onConversation,
  };
  const { rerender } = await render(<Composer {...props} />);
  await fireEvent.press(screen.getByLabelText('Start a conversation'));
  expect(onConversation).toHaveBeenCalled();
  await rerender(<Composer {...props} draft="hello" />);
  expect(screen.queryByLabelText('Start a conversation')).toBeNull();
});

describe('attachments (slice 7)', () => {
  const file = (over: Record<string, unknown>) => ({
    id: 'a1', uri: 'file:///p.jpg', name: 'receipt.jpg', mimeType: 'image/jpeg', size: 10, kind: 'image', state: 'ready',
    uploadId: 'up-1', ...over,
  }) as never;
  const props = (over: Record<string, unknown> = {}) => ({
    enabled: true, draft: '', setDraft: jest.fn(), onSendText: jest.fn(),
    onRecordStart: jest.fn(), onRecording: jest.fn().mockResolvedValue(null),
    onAttach: jest.fn(), onRemoveAttachment: jest.fn(), onRetryAttachment: jest.fn(), ...over,
  });

  it('the paperclip opens the choice, and a ready file alone can be sent', async () => {
    const p = props({ attachments: [file({})] });
    await render(<Composer {...p} />);
    await fireEvent.press(screen.getByLabelText('Attach a photo or file'));
    expect(p.onAttach).toHaveBeenCalled();
    expect(screen.getByTestId('attach-tray')).toBeTruthy();
    await fireEvent.press(screen.getByLabelText('Send'));
    expect(p.onSendText).toHaveBeenCalled();
  });

  it('Send waits while a file is still uploading', async () => {
    await render(<Composer {...props({ draft: 'look', attachments: [file({ state: 'uploading', uploadId: undefined })] })} />);
    expect(screen.getByLabelText('Send (waiting for a file to upload)')).toBeDisabled();
  });

  it('a refused file says why, is retried on a tap and removed with its button', async () => {
    const p = props({ attachments: [file({ state: 'failed', error: 'over 20 MB', uploadId: undefined })] });
    await render(<Composer {...p} />);
    expect(screen.getByText('over 20 MB')).toBeTruthy();
    expect(screen.queryByLabelText('Send')).toBeNull();             // nothing ready, nothing typed
    await fireEvent.press(screen.getByRole('button', { name: /receipt\.jpg, not sent/ }));
    expect(p.onRetryAttachment).toHaveBeenCalledWith('a1');
    await fireEvent.press(screen.getByLabelText('Remove receipt.jpg'));
    expect(p.onRemoveAttachment).toHaveBeenCalledWith('a1');
  });

  it('five files is the most a message carries', async () => {
    const five = [1, 2, 3, 4, 5].map((n) => file({ id: `a${n}` }));
    await render(<Composer {...props({ attachments: five })} />);
    expect(screen.getByLabelText('Attach a photo or file')).toBeDisabled();
  });

  it('no tray while recording', async () => {
    mockRecorder.active = true;
    await render(<Composer {...props({ attachments: [file({})] })} />);
    expect(screen.queryByTestId('attach-tray')).toBeNull();
  });
});

it('tapping a file that is fine does nothing', async () => {
  const onRetry = jest.fn();
  await render(<Composer enabled draft="" setDraft={jest.fn()} onSendText={jest.fn()} onRecordStart={jest.fn()}
    onRecording={jest.fn()} onAttach={jest.fn()} onRetryAttachment={onRetry}
    attachments={[{ id: 'a1', uri: 'u', name: 'ok.jpg', mimeType: 'image/jpeg', size: 1, kind: 'image', state: 'ready', uploadId: 'up' }]} />);
  await fireEvent.press(screen.getByLabelText('ok.jpg, ready'));
  expect(onRetry).not.toHaveBeenCalled();
});

it('offers Stop instead of the mic while JARVIS is writing, and Send still works when typing', async () => {
  const onStop = jest.fn(), base = { enabled: true, setDraft: jest.fn(), onSendText: jest.fn(),
    onRecordStart: jest.fn(), onRecording: jest.fn().mockResolvedValue(null), onStop };
  const view = await render(<Composer {...base} draft="" />);
  expect(screen.queryByLabelText('Record a voice message')).toBeNull();
  await fireEvent.press(screen.getByLabelText('Stop JARVIS'));
  expect(onStop).toHaveBeenCalledTimes(1);
  await view.rerender(<Composer {...base} draft="actually, ignore that" />);
  expect(screen.getByLabelText('Send')).toBeTruthy();
  await view.rerender(<Composer {...base} onStop={undefined} draft="" />);
  expect(screen.queryByLabelText('Stop JARVIS')).toBeNull();
  expect(screen.getByLabelText('Record a voice message')).toBeTruthy();
});
