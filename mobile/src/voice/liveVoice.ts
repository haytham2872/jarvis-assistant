// The phone's side of the live voice socket (server: WS /mobile/v1/voice/live). Microphone
// buffers go out as binary frames as they come; the server sends back the words so far ("heard")
// and, once it decides the turn is over (or we send {"type":"end"}), the submitted turn.
// Pure: the socket class is injected, so jest drives it with a fake.
import type { RequestStatus } from '../chat/types';

export type LiveResult =
  | { kind: 'turn'; requestId: string; text: string; status: RequestStatus }
  | { kind: 'empty' }              // nothing heard, no turn
  | { kind: 'no_speech' }          // conversation mode: 8 s of silence at the start, no turn
  | { kind: 'failed'; reason: string }
  | { kind: 'cancelled' };

type WSWithHeaders = new (url: string, protocols?: string | string[] | null,
  options?: { headers: Record<string, string> }) => WebSocket;

export type LiveOptions = {
  url: string;
  headers: Record<string, string>;
  onHeard?: (text: string) => void;
  WS?: WSWithHeaders;
  /** After end(): how long to wait for the server's "turn" before giving up. */
  timeoutMs?: number;
};

export const LIVE_END_TIMEOUT_MS = 20_000;

export class LiveVoice {
  readonly result: Promise<LiveResult>;
  private ws: WebSocket | null = null;
  private opened = false;
  private queue: (ArrayBuffer | string)[] = [];
  private settle!: (r: LiveResult) => void;
  private settled = false;
  private timer: ReturnType<typeof setTimeout> | null = null;

  constructor(private o: LiveOptions) {
    this.result = new Promise<LiveResult>((resolve) => { this.settle = resolve; });
    try {
      const WS = o.WS ?? (WebSocket as unknown as WSWithHeaders);
      const ws = new WS(o.url, null, { headers: o.headers });
      this.ws = ws;
      ws.binaryType = 'arraybuffer';
      ws.onopen = () => {
        this.opened = true;
        for (const item of this.queue) ws.send(item);
        this.queue = [];
      };
      ws.onmessage = (m: { data: unknown }) => this.onMessage(m.data);
      ws.onclose = () => this.finish({ kind: 'failed', reason: 'The live connection closed.' });
      ws.onerror = () => undefined;          // onclose follows
    } catch {
      this.finish({ kind: 'failed', reason: 'The live connection could not open.' });
    }
  }

  /** One microphone buffer. Held until the socket opens. */
  push(buf: ArrayBuffer): void {
    this.out(buf);
  }

  /** Commit the turn now. */
  end(): void {
    this.out('{"type":"end"}');
    if (!this.settled && !this.timer) {
      this.timer = setTimeout(() => this.finish({ kind: 'failed', reason: 'The server did not answer in time.' }),
        this.o.timeoutMs ?? LIVE_END_TIMEOUT_MS);
    }
  }

  cancel(): void {
    if (this.settled) return;
    if (this.opened) this.ws?.send('{"type":"cancel"}');
    this.finish({ kind: 'cancelled' });
  }

  private out(item: ArrayBuffer | string): void {
    if (this.settled || !this.ws) return;
    if (this.opened) this.ws.send(item);
    else this.queue.push(item);
  }

  private onMessage(data: unknown): void {
    let m: Record<string, unknown>;
    try {
      m = JSON.parse(String(data));
    } catch {
      return;
    }
    switch (m.type) {
      case 'heard':
        if (!this.settled && typeof m.text === 'string') this.o.onHeard?.(m.text);
        break;
      case 'turn':
        this.finish({ kind: 'turn', requestId: String(m.request_id), text: String(m.text ?? ''),
          status: m.status as RequestStatus });
        break;
      case 'empty':
        this.finish({ kind: 'empty' });
        break;
      case 'no_speech':
        this.finish({ kind: 'no_speech' });
        break;
      case 'error':
        this.finish({ kind: 'failed', reason: typeof m.detail === 'string' ? m.detail : 'The server reported an error.' });
        break;
      default:                                // "ready" and anything newer
    }
  }

  private finish(r: LiveResult): void {
    if (this.settled) return;
    this.settled = true;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    this.queue = [];
    const ws = this.ws;
    this.ws = null;
    this.settle(r);
    try {
      ws?.close();
    } catch {
      /* already closed */
    }
  }
}

/** Level of an int16 little-endian PCM buffer in dBFS, like iOS metering: -160 (silence) to 0. */
export function int16Level(buf: ArrayBuffer): number {
  const n = Math.floor(buf.byteLength / 2);
  if (!n) return -160;
  const view = new DataView(buf);
  let sum = 0;
  for (let i = 0; i < n; i++) {
    const s = view.getInt16(i * 2, true) / 32768;
    sum += s * s;
  }
  const rms = Math.sqrt(sum / n);
  return rms > 0 ? Math.max(-160, Math.min(0, 20 * Math.log10(rms))) : -160;
}
