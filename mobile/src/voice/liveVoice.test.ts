// The phone's side of the live voice socket (WS /voice/live): what goes out, and what each server
// message means. A fake socket stands in for the network.
import { int16Level, LiveVoice } from './liveVoice';

class FakeWS {
  static last: FakeWS;
  sent: (string | ArrayBuffer)[] = [];
  closed = false;
  onopen: (() => void) | null = null;
  onmessage: ((m: { data: unknown }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  binaryType = '';
  constructor(public url: string, public protocols: unknown, public options: { headers: Record<string, string> }) {
    FakeWS.last = this;
  }
  send(d: string | ArrayBuffer) { this.sent.push(d); }
  close() { this.closed = true; }
  open() { this.onopen?.(); }
  say(msg: object) { this.onmessage?.({ data: JSON.stringify(msg) }); }
  drop() { this.onclose?.(); }
}

const buf = (n: number) => new Uint8Array([n, 0]).buffer;
const make = (extra: Partial<ConstructorParameters<typeof LiveVoice>[0]> = {}) =>
  new LiveVoice({ url: 'wss://j.example/mobile/v1/voice/live?request_id=req-1', headers: { Authorization: 'Bearer t' },
    WS: FakeWS as never, ...extra });

beforeEach(() => jest.useFakeTimers());
afterEach(() => jest.useRealTimers());

it('opens the given url with the auth header', () => {
  make();
  expect(FakeWS.last.url).toBe('wss://j.example/mobile/v1/voice/live?request_id=req-1');
  expect(FakeWS.last.options).toEqual({ headers: { Authorization: 'Bearer t' } });
});

it('queues buffers sent before the socket opens and flushes them in order', () => {
  const v = make();
  v.push(buf(1));
  v.push(buf(2));
  expect(FakeWS.last.sent).toEqual([]);
  FakeWS.last.open();
  v.push(buf(3));
  expect(FakeWS.last.sent.map((b) => new Uint8Array(b as ArrayBuffer)[0])).toEqual([1, 2, 3]);
});

it('end() sends {"type":"end"} after anything queued', () => {
  const v = make();
  v.push(buf(1));
  v.end();
  FakeWS.last.open();
  expect(FakeWS.last.sent[1]).toBe('{"type":"end"}');
});

it('heard updates the callback with all the text so far', () => {
  const heard = jest.fn();
  make({ onHeard: heard });
  FakeWS.last.open();
  FakeWS.last.say({ type: 'heard', text: 'call' });
  FakeWS.last.say({ type: 'heard', text: 'call Samir' });
  expect(heard).toHaveBeenLastCalledWith('call Samir');
});

it('turn resolves the session', async () => {
  const v = make();
  FakeWS.last.open();
  const status = { request_id: 'req-1', state: 'queued' };
  FakeWS.last.say({ type: 'turn', request_id: 'req-1', text: 'call Samir', status });
  await expect(v.result).resolves.toEqual({ kind: 'turn', requestId: 'req-1', text: 'call Samir', status });
  expect(FakeWS.last.closed).toBe(true);
});

it('empty and no_speech resolve as themselves', async () => {
  const a = make();
  FakeWS.last.open();
  FakeWS.last.say({ type: 'empty' });
  await expect(a.result).resolves.toEqual({ kind: 'empty' });
  const b = make();
  FakeWS.last.open();
  FakeWS.last.say({ type: 'no_speech' });
  await expect(b.result).resolves.toEqual({ kind: 'no_speech' });
});

it('an error message, or a socket that closes before a turn, is a failure', async () => {
  const a = make();
  FakeWS.last.open();
  FakeWS.last.say({ type: 'error', detail: 'boom' });
  await expect(a.result).resolves.toMatchObject({ kind: 'failed' });
  const b = make();
  FakeWS.last.drop();
  await expect(b.result).resolves.toMatchObject({ kind: 'failed' });
});

it('a socket that cannot even be built is a failure', async () => {
  const Broken = function () { throw new Error('no'); } as never;
  const v = new LiveVoice({ url: 'x', headers: {}, WS: Broken });
  await expect(v.result).resolves.toMatchObject({ kind: 'failed' });
});

it('the first result wins: a close after a turn changes nothing', async () => {
  const v = make();
  FakeWS.last.open();
  FakeWS.last.say({ type: 'turn', request_id: 'req-1', text: 'hi', status: {} });
  FakeWS.last.drop();
  await expect(v.result).resolves.toMatchObject({ kind: 'turn' });
});

it('cancel sends cancel and resolves cancelled', async () => {
  const v = make();
  FakeWS.last.open();
  v.cancel();
  expect(FakeWS.last.sent).toContain('{"type":"cancel"}');
  await expect(v.result).resolves.toEqual({ kind: 'cancelled' });
  expect(FakeWS.last.closed).toBe(true);
});

it('20 s after end() without a turn is a failure', async () => {
  const v = make();
  FakeWS.last.open();
  v.end();
  jest.advanceTimersByTime(19_999);
  let done = false;
  void v.result.then(() => { done = true; });
  await Promise.resolve();
  expect(done).toBe(false);
  jest.advanceTimersByTime(2);
  await expect(v.result).resolves.toMatchObject({ kind: 'failed' });
});

describe('int16Level', () => {
  const pcm = (...s: number[]) => new Int16Array(s).buffer;
  it('is dBFS like iOS metering: 0 at full scale, -160 for silence', () => {
    expect(int16Level(pcm(32767, -32768, 32767))).toBeCloseTo(0, 0);
    expect(int16Level(pcm(0, 0, 0))).toBe(-160);
    expect(int16Level(new ArrayBuffer(0))).toBe(-160);
  });
  it('half scale is about -6 dB', () => {
    expect(int16Level(pcm(16384, -16384))).toBeCloseTo(-6, 0);
  });
});
