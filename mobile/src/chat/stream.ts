// WS /events: live events with reconnect. On a stale position the server says "resync" and
// the caller fetches a snapshot; a dropped socket backs off and comes back from where it was.
import { authHeaders, endpoint, wsUrl, type ServerConfig } from '../core/api';
import type { HubEvent } from './types';

export const BACKOFF_MS = [1_000, 2_000, 5_000, 10_000];

export type StreamHandlers = {
  position: () => { epoch: string; seq: number };
  onEvent: (evt: HubEvent) => void;
  onResync: () => void;
  onStatus: (live: boolean) => void;
};

type WSWithHeaders = new (url: string, protocols?: string | string[] | null,
  options?: { headers: Record<string, string> }) => WebSocket;

export function eventsUrl(cfg: ServerConfig, epoch: string, seq: number): string {
  const q = epoch ? `?epoch=${encodeURIComponent(epoch)}&after=${seq}` : '';
  return wsUrl(endpoint(cfg.baseUrl, '/events')) + q;
}

export class EventStream {
  private ws: WebSocket | null = null;
  private attempt = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private stopped = true;

  constructor(private cfg: ServerConfig, private h: StreamHandlers, private WS = WebSocket as unknown as WSWithHeaders) {}

  start(): void {
    this.stopped = false;
    this.open();
  }

  /** Reconnect now (the app came back to the foreground). */
  kick(): void {
    if (this.stopped) return;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    if (!this.ws) this.open();
  }

  stop(): void {
    this.stopped = true;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    const ws = this.ws;
    this.ws = null;
    ws?.close();
  }

  private open(): void {
    const { epoch, seq } = this.h.position();
    let ws: WebSocket;
    try {
      ws = new this.WS(eventsUrl(this.cfg, epoch, seq), null, { headers: authHeaders(this.cfg.token) });
    } catch {
      this.retry();
      return;
    }
    this.ws = ws;
    ws.onopen = () => {
      this.attempt = 0;
      this.h.onStatus(true);
    };
    ws.onmessage = (m: { data: unknown }) => {
      let evt: HubEvent;
      try {
        evt = JSON.parse(String(m.data));
      } catch {
        return;
      }
      if (evt.type === 'resync') this.h.onResync();
      else if (evt.type !== 'keepalive') this.h.onEvent(evt);
    };
    ws.onclose = () => {
      if (this.ws !== ws) return;          // an old socket closing after a kick
      this.ws = null;
      this.h.onStatus(false);
      this.retry();
    };
    ws.onerror = () => undefined;          // onclose follows
  }

  private retry(): void {
    if (this.stopped) return;
    const wait = BACKOFF_MS[Math.min(this.attempt, BACKOFF_MS.length - 1)];
    this.attempt += 1;
    this.timer = setTimeout(() => {
      this.timer = null;
      if (!this.stopped) this.open();
    }, wait);
  }
}
