// HTTP calls for the conversation (backend/mobile/routes.py).
import { File, UploadType } from 'expo-file-system';

import { authHeaders, endpoint, ExplainedError, type ServerConfig } from '../core/api';
import type { SpeechPlan } from '../voice/speechQueue';
import type {
  ActivityView, Approval, ChatSummary, Exchange, InboxView, NoticeRoute, NoticeSettings, Outcome, PortfolioView,
  RequestStatus, Snapshot, TapResult, TodayView, UpcomingItem,
} from './types';
import type { GoalDetail, GoalEvidence, GoalFile, GoalsView, HubView } from './types';
import type { ChartRange, HoldingView, ProposalResult, TotalHistory } from './types';
import type { WeekChange, WeekContext, WeekView } from './types';
import type { RecordView } from './types';
import type { OvernightView, VaultNote } from './types';
import type { MindView, PortraitView } from './types';

export class HttpError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

/** The cached representation used for an HTTP conditional GET. */
export type Conditional<T> = { etag?: string; cached?: T; onEtag?: (etag: string | undefined) => void };

async function call<T>(cfg: ServerConfig, path: string, init: RequestInit = {}, cache?: Conditional<T>): Promise<T> {
  const res = await fetch(endpoint(cfg.baseUrl, path), {
    ...init,
    headers: { ...authHeaders(cfg.token), 'Content-Type': 'application/json', ...(init.headers ?? {}),
      ...(cache?.etag && cache.cached !== undefined ? { 'If-None-Match': cache.etag } : {}) },
  });
  if (res.status === 304 && cache?.cached !== undefined) {
    cache.onEtag?.(res.headers.get('etag') ?? cache.etag);
    return cache.cached;
  }
  const type = res.headers.get('content-type') ?? '';
  if (!type.startsWith('application/json')) {
    throw new ExplainedError(`That address answered with ${type.split(';')[0] || 'an unknown type'}, not JARVIS.`);
  }
  const body = await res.json();
  if (!res.ok) throw new HttpError(res.status, typeof body?.detail === 'string' ? body.detail : `HTTP ${res.status}`);
  cache?.onEtag?.(res.headers.get('etag') ?? undefined);
  return body as T;
}

export const getSnapshot = (cfg: ServerConfig) => call<Snapshot>(cfg, '/snapshot');

export type CommandInfo = { name: string; usage: string; description: string; choices: string[] };

// The list only changes with a server release: one fetch per server for the life of the app.
const commandCache = new Map<string, Promise<CommandInfo[]>>();

/** GET /commands, cached for the session (a failure is not cached). */
export function getCommands(cfg: ServerConfig): Promise<CommandInfo[]> {
  const key = `${cfg.baseUrl}|${cfg.token}`;
  let hit = commandCache.get(key);
  if (!hit) {
    hit = call<{ commands: CommandInfo[] }>(cfg, '/commands').then((r) => r.commands);
    commandCache.set(key, hit);
    hit.catch(() => commandCache.delete(key));
  }
  return hit;
}

/** POST /command: runs a slash command and returns its reply (HttpError 400: not a command). */
export const runCommand = (cfg: ServerConfig, text: string) =>
  call<{ reply: string }>(cfg, '/command', { method: 'POST', body: JSON.stringify({ text }) }).then((r) => r.reply);

/** `replyTo`: the Inbox message he is answering (slice 4); the turn is shown it. */
export const sendMessage = (cfg: ServerConfig, request_id: string, text: string, input: 'text' | 'voice' = 'text',
  replyTo?: number, uploads?: string[]) =>
  call<RequestStatus & { problems?: string[] }>(cfg, '/messages', {
    method: 'POST', body: JSON.stringify({ request_id, text, input, ...(replyTo ? { reply_to: replyTo } : {}),
      ...(uploads?.length ? { uploads } : {}) }),
  });

export type Uploaded = { upload_id: string; name: string; kind: 'image' | 'document'; bytes: number };

/** POST /uploads (slice 7): one file's raw bytes, held by the server until the message that
 * names it is sent. A file it can't read is refused here (HttpError 415/413), before sending. */
export async function uploadFile(cfg: ServerConfig, uri: string, name: string, mimeType: string): Promise<Uploaded> {
  const res = await new File(uri).upload(endpoint(cfg.baseUrl, `/uploads?name=${encodeURIComponent(name)}`), {
    headers: { ...authHeaders(cfg.token), 'Content-Type': mimeType },
    mimeType,
    uploadType: UploadType.BINARY_CONTENT,
    sessionType: 'foreground',
  });
  let body: Record<string, unknown> = {};
  try {
    body = JSON.parse(res.body);
  } catch {
    throw new ExplainedError('That address answered with something other than JARVIS.');
  }
  if (res.status < 200 || res.status >= 300) {
    throw new HttpError(res.status, typeof body.detail === 'string' ? body.detail : `HTTP ${res.status}`);
  }
  return body as unknown as Uploaded;
}

/** After a send whose answer never came back: did the server get it? null = it did not. */
export async function findRequest(cfg: ServerConfig, request_id: string): Promise<RequestStatus | null> {
  try {
    return await call<RequestStatus>(cfg, `/requests/${encodeURIComponent(request_id)}`);
  } catch (e) {
    if (e instanceof HttpError && e.status === 404) return null;
    throw e;
  }
}

export const newChat = (cfg: ServerConfig) => call<{ message: string }>(cfg, '/chats', { method: 'POST' });

export const listChats = (cfg: ServerConfig) => call<{ chats: ChatSummary[] }>(cfg, '/chats');

export const getChat = (cfg: ServerConfig, sessionId: string) =>
  call<{ session_id: string; exchanges: Exchange[] }>(cfg, `/chats/${encodeURIComponent(sessionId)}`);

export type VoiceResult = { transcript: string | null; status: RequestStatus; stt: Record<string, unknown> | null };

/** POST /recordings: the raw file, transcribed and submitted under `request_id` (a retry with
 * the same id returns the original). 422 means nothing was heard: no message was created. */
export async function sendVoice(cfg: ServerConfig, request_id: string, uri: string, spoken = false,
  replyTo?: number): Promise<VoiceResult> {
  const path = `/recordings?request_id=${encodeURIComponent(request_id)}${spoken ? '&spoken=1' : ''}`
    + (replyTo ? `&reply_to=${replyTo}` : '');
  const res = await new File(uri).upload(endpoint(cfg.baseUrl, path), {
    headers: { ...authHeaders(cfg.token), 'Content-Type': 'audio/mp4' },
    mimeType: 'audio/mp4',
    uploadType: UploadType.BINARY_CONTENT,
    sessionType: 'foreground',
  });
  let body: Record<string, unknown> = {};
  try {
    body = JSON.parse(res.body);
  } catch {
    throw new ExplainedError('That address answered with something other than JARVIS.');
  }
  if (res.status < 200 || res.status >= 300) {
    throw new HttpError(res.status, typeof body.detail === 'string' ? body.detail : `HTTP ${res.status}`);
  }
  return body as unknown as VoiceResult;
}

/** The Stop button: stops the answer JARVIS is writing now. */
export const stopAnswer = (cfg: ServerConfig) => call<{ stopped: boolean }>(cfg, '/stop', { method: 'POST' });

/** The phone started recording: the server loads its transcriber now. Never throws. */
export async function warm(cfg: ServerConfig): Promise<void> {
  try {
    await call(cfg, '/warm', { method: 'POST' });
  } catch {
    /* only an optimisation */
  }
}

/** GET /speech/plan: how the final answer `source` ('req:<id>' or 'ex:<id>') is spoken. */
export const getSpeechPlan = (cfg: ServerConfig, source: string) =>
  call<SpeechPlan>(cfg, `/speech/plan?source=${encodeURIComponent(source)}`);

/** GET /speech/clip: one clip as MP3 bytes. HttpError 503 (monthly allowance used) or 502
 * (the voice failed) means the iPhone's own voice should read it. */
export async function fetchClipBytes(cfg: ServerConfig, source: string, revision: string, seq: number): Promise<Uint8Array> {
  const q = `source=${encodeURIComponent(source)}&revision=${encodeURIComponent(revision)}&seq=${seq}`;
  const res = await fetch(endpoint(cfg.baseUrl, `/speech/clip?${q}`), { headers: authHeaders(cfg.token) });
  if (!res.ok) {
    let detail = '';
    try {
      const body = await res.json();
      detail = typeof body?.detail === 'string' ? body.detail : '';
    } catch {
      /* not JSON */
    }
    throw new HttpError(res.status, detail || `HTTP ${res.status}`);
  }
  const type = res.headers.get('content-type') ?? '';
  if (!type.startsWith('audio/')) throw new ExplainedError(`Expected audio, got ${type.split(';')[0] || 'nothing'}.`);
  return new Uint8Array(await res.arrayBuffer());
}

/** POST /approvals/{id}: the owner's decision, the same one a Discord tap makes (at most once). */
export const decideApproval = (cfg: ServerConfig, id: number, approve: boolean) =>
  call<{ text: string; approval: Approval | null }>(cfg, `/approvals/${id}`, {
    method: 'POST', body: JSON.stringify({ decision: approve ? 'approve' : 'deny' }),
  });

/** GET /activity: what JARVIS is running, what waits on the owner, what is scheduled, what he did. */
export const getActivity = (cfg: ServerConfig, cache?: Conditional<ActivityView>) => call<ActivityView>(cfg, '/activity', {}, cache);

export type PauseResult = { text: string; activity: ActivityView };

/** POST /pause: the same as /pause on Discord. `hours` null pauses until resumed; `all` also
 * holds reminders and the morning brief. */
export const pauseWork = (cfg: ServerConfig, hours: number | null, all: boolean) =>
  call<PauseResult>(cfg, '/pause', { method: 'POST', body: JSON.stringify({ hours, all }) });

/** POST /resume: ends the pause and says what it skipped. */
export const resumeWork = (cfg: ServerConfig) => call<PauseResult>(cfg, '/resume', { method: 'POST' });

/** GET /today: his planned blocks and what became of each, yesterday's still open, the calendar,
 * today's reminders and timed checks, and the week. */
export const getToday = (cfg: ServerConfig, cache?: Conditional<TodayView>) => call<TodayView>(cfg, '/today', {}, cache);

/** POST /today/blocks/{uid}: what became of one block, the record a check-in reply makes. A skip
 * may come back with a new slot to approve (`asked`, and the block's `offer`). */
export const tapBlock = (cfg: ServerConfig, uid: string, outcome: Exclude<Outcome, 'unknown'>) =>
  call<TapResult>(cfg, `/today/blocks/${encodeURIComponent(uid)}`, { method: 'POST', body: JSON.stringify({ outcome }) });

/** GET /portfolio: the ledger, read-only. A split waiting for his yes is decided with
 * decideApproval, the same request Discord shows. */
export const getPortfolio = (cfg: ServerConfig, cache?: Conditional<PortfolioView>) => call<PortfolioView>(cfg, '/portfolio', {}, cache);

// ------------------------------------------------------------------ next plan slice 3: the living portfolio

/** GET /portfolio/history: the total in the base currency over a range, with what it cost. */
export const getPortfolioHistory = (cfg: ServerConfig, range: ChartRange, cache?: Conditional<TotalHistory>) =>
  call<TotalHistory>(cfg, `/portfolio/history?range=${range}`, {}, cache);

/** GET /portfolio/{symbol}: one stock's price with his trades, levels and forecasts on it. */
export const getHolding = (cfg: ServerConfig, symbol: string, range: ChartRange, cache?: Conditional<HoldingView>) =>
  call<HoldingView>(cfg, `/portfolio/${encodeURIComponent(symbol)}?range=${range}`, {}, cache);

/** POST /portfolio/{symbol}/watch: watch for a close below or above a level he confirmed. */
export const proposeWatch = (cfg: ServerConfig, symbol: string, direction: 'below' | 'above', level: number) =>
  call<ProposalResult>(cfg, `/portfolio/${encodeURIComponent(symbol)}/watch`, {
    method: 'POST', body: JSON.stringify({ direction, level }),
  });

/** POST /portfolio/{symbol}/condition: a price condition on one of his open theses. */
export const proposeCondition = (cfg: ServerConfig, symbol: string, thesisId: number, direction: 'wrong_if' | 'right_if',
  kind: 'price_below' | 'price_above', level: number) =>
  call<ProposalResult>(cfg, `/portfolio/${encodeURIComponent(symbol)}/condition`, {
    method: 'POST', body: JSON.stringify({ thesis_id: thesisId, direction, kind, level }),
  });

// ------------------------------------------------------------------ slice 4: the week (next plan)
export const getWeek = (cfg: ServerConfig, start: string, days = 3, cache?: Conditional<WeekView>) =>
  call<WeekView>(cfg, `/week?start=${encodeURIComponent(start)}&days=${days}`, {}, cache);
export const moveWeekBlock = (cfg: ServerConfig, uid: string, context: WeekContext, day: string, start: string) =>
  call<WeekChange>(cfg, `/week/blocks/${encodeURIComponent(uid)}/move`, {
    method: 'POST', body: JSON.stringify({ ...context, day, start }),
  });
export const addWeekBlock = (cfg: ServerConfig, context: WeekContext, day: string, start: string, kind: string, minutes: number) =>
  call<WeekChange>(cfg, '/week/blocks', { method: 'POST', body: JSON.stringify({ ...context, day, start, kind, minutes }) });
export const undoWeek = (cfg: ServerConfig, context: WeekContext) =>
  call<WeekChange>(cfg, '/week/undo', { method: 'POST', body: JSON.stringify(context) });
export const applyWeek = (cfg: ServerConfig, context: WeekContext, week: string, planId: number) =>
  call<WeekChange>(cfg, '/week/apply', { method: 'POST', body: JSON.stringify({ ...context, week, plan_id: planId }) });

// ------------------------------------------------------------------ slice 4: Inbox and notifications

const pushHeader = (token?: string | null): Record<string, string> => (token ? { 'X-Push-Token': token } : {});

/** POST /inbox/feedback: a thumb on a research finding ('none' clears it). */
export const sendInboxFeedback = (cfg: ServerConfig, id: number, vote: 'up' | 'down' | 'none') =>
  call<{ vote: 'up' | 'down' | null }>(cfg, '/inbox/feedback', { method: 'POST', body: JSON.stringify({ id, vote }) });

/** GET /inbox: JARVIS's own messages, newest first; `before` pages back. */
export function getInbox(cfg: ServerConfig, pushToken?: string | null, opts: { before?: number; limit?: number } = {}, cache?: Conditional<InboxView>) {
  const q = [opts.before ? `before=${opts.before}` : '', opts.limit ? `limit=${opts.limit}` : ''].filter(Boolean).join('&');
  return call<InboxView>(cfg, `/inbox${q ? `?${q}` : ''}`, { headers: pushHeader(pushToken) }, cache);
}

/** POST /inbox/read: everything up to `upTo` has been seen. */
export const markInboxRead = (cfg: ServerConfig, upTo: number) =>
  call<{ unread: number }>(cfg, '/inbox/read', { method: 'POST', body: JSON.stringify({ up_to: upTo }) });

export const getNoticeSettings = (cfg: ServerConfig, pushToken?: string | null) =>
  call<NoticeSettings>(cfg, '/notices/settings', { headers: pushHeader(pushToken) });

/** PUT /notices/settings: where JARVIS's own messages go. */
export const setNoticeRoute = (cfg: ServerConfig, route: NoticeRoute, pushToken?: string | null) =>
  call<NoticeSettings>(cfg, '/notices/settings', {
    method: 'PUT', body: JSON.stringify({ route }), headers: pushHeader(pushToken),
  });

/** POST /push: register this phone's push token; the server sends a test notification. */
export const registerPushToken = (cfg: ServerConfig, pushToken: string) =>
  call<NoticeSettings & { test_sent: boolean }>(cfg, '/push', { method: 'POST', headers: pushHeader(pushToken) });

/** POST /push/confirm: the test notification showed on this phone. */
export const confirmPushToken = (cfg: ServerConfig, pushToken: string) =>
  call<NoticeSettings>(cfg, '/push/confirm', { method: 'POST', headers: pushHeader(pushToken) });

/** GET /upcoming: what this phone should notify about by itself over the next week. */
export const getUpcoming = (cfg: ServerConfig, pushToken?: string | null) =>
  call<NoticeSettings & { items: UpcomingItem[] }>(cfg, '/upcoming', { headers: pushHeader(pushToken) });

export const getHub = (cfg: ServerConfig, cache?: Conditional<HubView>, since?: number | null) =>
  call<HubView>(cfg, `/hub${since == null ? '' : `?since=${encodeURIComponent(since)}`}`, {}, cache);

// slice 6: while you slept and the shared read-only note screen.
export const getOvernight = (cfg: ServerConfig, since?: number | null, cache?: Conditional<OvernightView>) =>
  call<OvernightView>(cfg, `/overnight${since == null ? '' : `?since=${encodeURIComponent(since)}`}`, {}, cache);
export const getVaultNote = (cfg: ServerConfig, folder: string, name: string, cache?: Conditional<VaultNote>) =>
  call<VaultNote>(cfg, `/vault/note?folder=${encodeURIComponent(folder)}&name=${encodeURIComponent(name)}`, {}, cache);

// slice 7: the mind map
export const getMind = (cfg: ServerConfig, cache?: Conditional<MindView>) => call<MindView>(cfg, '/mind', {}, cache);
export const getPortrait = (cfg: ServerConfig, cache?: Conditional<PortraitView>) => call<PortraitView>(cfg, '/mind/portrait', {}, cache);

// slice 5: the track record
export const getRecord = (cfg: ServerConfig, cache?: Conditional<RecordView>) => call<RecordView>(cfg, '/record', {}, cache);

export const getGoals = (cfg: ServerConfig, cache?: Conditional<GoalsView>) => call<GoalsView>(cfg, '/goals', {}, cache);
const goalPath = (id: string) => `/goals/${encodeURIComponent(id)}`;
export const getGoal = (cfg: ServerConfig, id: string, cache?: Conditional<GoalDetail>) => call<GoalDetail>(cfg, goalPath(id), {}, cache);
export const getGoalFile = (cfg: ServerConfig, id: string, name: string) => call<GoalFile>(cfg, `${goalPath(id)}/files/${encodeURIComponent(name)}`);
export const getGoalEvidence = (cfg: ServerConfig, id: string, evidence: string) =>
  call<GoalEvidence>(cfg, `${goalPath(id)}/evidence/${encodeURIComponent(evidence)}`);
type GoalChange = { text: string; goal: GoalDetail };
export const controlGoal = (cfg: ServerConfig, id: string, action: 'pause' | 'resume' | 'cancel') =>
  call<GoalChange>(cfg, `${goalPath(id)}/control`, { method: 'POST', body: JSON.stringify({ action }) });
export const raiseGoalBudget = (cfg: ServerConfig, id: string, usd: number) =>
  call<GoalChange>(cfg, `${goalPath(id)}/budget`, { method: 'POST', body: JSON.stringify({ usd }) });
export const answerGoal = (cfg: ServerConfig, wait_id: string, text: string) =>
  call<GoalChange>(cfg, '/goals/answer', { method: 'POST', body: JSON.stringify({ wait_id, text }) });
export const decideGoalProposal = (cfg: ServerConfig, id: string, start: boolean) =>
  call<{ text: string }>(cfg, `/goals/proposals/${encodeURIComponent(id)}/${start ? 'start' : 'decline'}`, { method: 'POST' });

/** Transcription only: the editable text is sent separately after the owner taps Send. */
export async function transcribeGoalAnswer(cfg: ServerConfig, waitId: string, uri: string): Promise<string> {
  const res = await new File(uri).upload(endpoint(cfg.baseUrl, `/goals/answer/voice?wait_id=${encodeURIComponent(waitId)}`), {
    headers: { ...authHeaders(cfg.token), 'Content-Type': 'audio/mp4' }, mimeType: 'audio/mp4',
    uploadType: UploadType.BINARY_CONTENT, sessionType: 'foreground',
  });
  let body;
  try { body = JSON.parse(res.body); } catch { throw new ExplainedError('That address answered with something other than JARVIS.'); }
  if (res.status < 200 || res.status >= 300) throw new HttpError(res.status, body.detail ?? `HTTP ${res.status}`);
  if (typeof body.transcript !== 'string') throw new ExplainedError('JARVIS did not return a transcript.');
  return body.transcript;
}
