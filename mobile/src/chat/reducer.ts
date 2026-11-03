// The conversation's state on the phone: pure, so it is tested without a phone.
//
// The server's records are the truth (a snapshot replaces what the phone believes); live
// events only fill in between snapshots. A request the phone sent but has not heard back
// about stays visible with its own state until the server says what became of it.
import type { FileChip } from '../attach/model';
import type { Approval, Exchange, HubEvent, Job, Mood, Notice, PlanStep, RequestState, RequestStatus, Snapshot, ToolUse } from './types';

export type LocalState = RequestState | 'sending' | 'send_failed' | 'transcribing';

export type LocalRequest = {
  request_id: string;
  text: string;
  sentAt: number;
  state: LocalState;
  answer: string | null;
  error?: string;
  steered?: boolean;
  input?: 'text' | 'voice';
  /** The recording on the phone, kept until the server has it, so a retry resends the same file. */
  audio?: string;
  /** Slice 7: the files sent with it, shown until the server's text names them. */
  files?: FileChip[];
  /** Files the server could not read, in words. */
  problems?: string[];
};

export type Connection = 'connecting' | 'live' | 'offline';

/** What JARVIS did on the way to an answer: what he said before a tool, and the tool. The
 * final answer drops that narration by design; the steps keep it (the owner, 14 October). */
export type Step = { kind: 'say'; text: string } | { kind: 'tool'; tool: string; detail?: string; outcome?: string };

export type ChatState = {
  exchanges: Exchange[];
  requests: Record<string, LocalRequest>;
  order: string[];
  drafts: Record<string, string>;
  activity: Record<string, string>;
  steps: Record<string, Step[]>;
  approvals: Record<number, Approval>;
  jobs: Record<number, Job>;
  /** The plan each request's turn is following, live (M4). */
  plans: Record<string, PlanStep[]>;
  epoch: string;
  seq: number;
  connection: Connection;
  loaded: boolean;
  /** Slice 4: the newest Inbox message announced live (the Inbox reads the rest itself). */
  lastNotice: Notice | null;
  /** Slice 6: JARVIS's mood, from the snapshot and each finished turn; it tints the orb. */
  mood: Mood | null;
};

export const initialChat: ChatState = {
  exchanges: [], requests: {}, order: [], drafts: {}, activity: {}, steps: {}, approvals: {}, jobs: {}, plans: {},
  epoch: '', seq: 0,
  connection: 'connecting', loaded: false, lastNotice: null, mood: null,
};

export type ChatAction =
  | { type: 'events'; events: HubEvent[] }
  | { type: 'snapshot'; snap: Snapshot }
  | { type: 'event'; evt: HubEvent }
  | { type: 'send'; request_id: string; text: string; at: number; files?: FileChip[] }
  | { type: 'send_voice'; request_id: string; at: number; audio: string }
  | { type: 'heard_nothing'; request_id: string }
  | { type: 'sent'; status: RequestStatus }
  | { type: 'send_failed'; request_id: string; error: string }
  | { type: 'connection'; value: Connection }
  | { type: 'approval'; approval: Approval }
  | { type: 'reset' };

const OUTCOMES: Record<string, LocalState> = {
  answered: 'answered', failed: 'failed', interrupted: 'interrupted', cancelled: 'cancelled',
};
const MAX_ACTIVITY = 2_000;   // characters of thinking kept per request: a glimpse, not a log

function merge(state: ChatState, s: RequestStatus): ChatState {
  const prev = state.requests[s.request_id];
  const req: LocalRequest = {
    request_id: s.request_id,
    // A voice request's text arrives with the transcript; a message with files comes back with the
    // server's line naming them added to his words, which is what the stored exchange holds.
    text: prev?.text && !(s.text && s.text !== prev.text && s.text.startsWith(prev.text)) ? prev.text : s.text || prev?.text || '',
    sentAt: prev?.sentAt ?? s.received_at * 1000,
    state: s.state,
    answer: s.answer ?? prev?.answer ?? null,
    steered: s.steered,
    input: s.input ?? prev?.input,
    files: prev?.files,
    problems: s.problems ?? prev?.problems,
  };
  const order = state.order.includes(s.request_id) ? state.order : [...state.order, s.request_id];
  return { ...state, requests: { ...state.requests, [s.request_id]: req }, order,
    steps: withReceipts(state.steps, s.request_id, s.tools) };
}

/** Steps seen live keep their narration and gain the receipts' outcomes; a request the phone
 * did not watch (reopened app) gets its tools from the receipts alone. */
function withReceipts(steps: Record<string, Step[]>, id: string, tools?: ToolUse[]): Record<string, Step[]> {
  if (!tools?.length) return steps;
  const live = steps[id];
  if (!live?.some((s) => s.kind === 'tool')) {
    return { ...steps, [id]: tools.map((t) => ({ kind: 'tool', tool: t.tool, detail: t.detail, outcome: t.outcome })) };
  }
  let i = 0;
  const annotated = live.map((s) => {
    if (s.kind !== 'tool') return s;
    const t = tools[i++];
    return t && t.tool === s.tool ? { ...s, outcome: t.outcome } : s;
  });
  return { ...steps, [id]: annotated };
}

function patch(state: ChatState, id: string, fields: Partial<LocalRequest>): ChatState {
  const prev = state.requests[id];
  if (!prev) return state;
  return { ...state, requests: { ...state.requests, [id]: { ...prev, ...fields } } };
}

function planStep(s: Partial<PlanStep>, i: number): PlanStep {
  const status = s.status === 'running' || s.status === 'done' || s.status === 'failed' ? s.status : 'pending';
  return { id: typeof s.id === 'number' ? s.id : i + 1, label: String(s.label ?? ''), tool: s.tool ?? null, status };
}

function byId<T extends { id: number }>(rows: T[]): Record<number, T> {
  const out: Record<number, T> = {};
  for (const r of rows) out[r.id] = r;
  return out;
}

/** Requests that still need him: waiting, or being carried out after his OK. */
export function waitingApprovals(state: ChatState): Approval[] {
  return Object.values(state.approvals)
    .filter((a) => a.state === 'pending' || a.state === 'executing')
    .sort((a, b) => a.created_at - b.created_at);
}

function without<T>(rec: Record<string, T>, key: string): Record<string, T> {
  if (!(key in rec)) return rec;
  const { [key]: _gone, ...rest } = rec;
  return rest;
}

export function chatReducer(state: ChatState, action: ChatAction): ChatState {
  if (action.type === 'events') {
    return action.events.reduce((s, evt) => chatReducer(s, { type: 'event', evt }), state);
  }
  switch (action.type) {
    case 'reset':
      return { ...initialChat, connection: state.connection };

    case 'connection':
      return { ...state, connection: action.value };

    case 'snapshot': {
      const { snap } = action;
      let next: ChatState = { ...state, exchanges: snap.exchanges, epoch: snap.epoch, seq: snap.seq, loaded: true };
      // The server's lists are the truth: a decision made elsewhere, or a job that finished
      // while the app was closed, is in them.
      if (snap.approvals) next = { ...next, approvals: byId(snap.approvals) };
      if (snap.jobs) next = { ...next, jobs: byId(snap.jobs) };
      if (snap.mood !== undefined) next = { ...next, mood: snap.mood };
      // The server knows every request it received; ones it never received (still sending,
      // or a send that failed) are the phone's own and stay as they are.
      const known = new Set(snap.requests.map((r) => r.request_id));
      const kept: Record<string, LocalRequest> = {};
      for (const id of next.order) {
        const r = next.requests[id];
        if (r && !known.has(id) && (r.state === 'sending' || r.state === 'send_failed' || r.state === 'transcribing')) kept[id] = r;
      }
      const before = next.requests;
      next = { ...next, requests: kept, order: next.order.filter((id) => id in kept) };
      for (const s of [...snap.requests].sort((a, b) => a.received_at - b.received_at)) next = merge(next, s);
      // Slice 7: what the phone alone knows about a request (the files he picked, the ones the
      // server could not read) outlives a snapshot that rebuilds it.
      for (const [id, old] of Object.entries(before)) {
        const r = next.requests[id];
        if (r && (old.files || old.problems)) {
          next.requests[id] = { ...r, files: r.files ?? old.files, problems: r.problems ?? old.problems };
        }
      }
      return next;
    }

    case 'send':
      return {
        ...state,
        requests: {
          ...state.requests,
          [action.request_id]: {
            request_id: action.request_id, text: action.text, sentAt: action.at, state: 'sending', answer: null,
            ...(action.files?.length ? { files: action.files } : {}),
          },
        },
        order: state.order.includes(action.request_id) ? state.order : [...state.order, action.request_id],
      };

    case 'send_voice':
      return {
        ...state,
        requests: {
          ...state.requests,
          [action.request_id]: {
            request_id: action.request_id, text: '', sentAt: action.at, state: 'transcribing', answer: null,
            input: 'voice', audio: action.audio,
          },
        },
        order: state.order.includes(action.request_id) ? state.order : [...state.order, action.request_id],
      };

    case 'heard_nothing':
      // Nothing was heard, so no message exists: the placeholder goes rather than staying empty.
      return { ...state, requests: without(state.requests, action.request_id),
        order: state.order.filter((id) => id !== action.request_id) };

    case 'sent':
      return merge(state, action.status);

    case 'send_failed':
      return patch(state, action.request_id, { state: 'send_failed', error: action.error });

    case 'approval':
      return { ...state, approvals: { ...state.approvals, [action.approval.id]: action.approval } };

    case 'event': {
      const { evt } = action;
      if (evt.epoch === state.epoch && evt.seq <= state.seq) return state;   // already seen
      let next: ChatState = { ...state, epoch: evt.epoch, seq: evt.seq };
      const id = evt.request_id ?? '';
      switch (evt.type) {
        case 'accepted':
          return id && (next.requests[id]?.state === 'sending' || next.requests[id]?.state === 'transcribing')
            ? patch(next, id, { state: 'queued', steered: !!evt.steers_run }) : next;
        case 'run_started':
          next = id ? patch(next, id, { state: 'running' }) : next;
          return id ? { ...next, drafts: { ...next.drafts, [id]: '' } } : next;
        case 'response':
          return id ? { ...next, drafts: { ...next.drafts, [id]: (next.drafts[id] ?? '') + (evt.delta ?? '') } } : next;
        case 'thinking': {
          if (!id) return next;
          const text = ((next.activity[id] ?? '') + (evt.delta ?? '')).slice(-MAX_ACTIVITY);
          return { ...next, activity: { ...next.activity, [id]: text } };
        }
        case 'tool_call': {
          if (!id) return next;
          const said = (next.drafts[id] ?? '').trim();
          const steps: Step[] = [...(next.steps[id] ?? []), ...(said ? [{ kind: 'say' as const, text: said }] : []),
            { kind: 'tool', tool: evt.tool ?? 'a tool', detail: evt.detail }];
          return { ...next, steps: { ...next.steps, [id]: steps }, drafts: { ...next.drafts, [id]: '' },
            activity: { ...next.activity, [id]: `Using ${evt.tool ?? 'a tool'}…` } };
        }
        case 'run_finished': {
          if (evt.mood !== undefined) next = { ...next, mood: evt.mood };
          if (!id) return next;
          next = patch(next, id, {
            state: OUTCOMES[evt.outcome ?? ''] ?? 'failed',
            answer: evt.answer ?? next.requests[id]?.answer ?? null,
            error: evt.error ?? undefined,
          });
          return { ...next, drafts: without(next.drafts, id), activity: without(next.activity, id) };
        }
        case 'plan':
        case 'plan_update':
          return id && evt.steps ? { ...next, plans: { ...next.plans, [id]: evt.steps.map(planStep) } } : next;
        case 'step_start':
        case 'step_done': {
          const plan = id ? next.plans[id] : undefined;
          if (!plan || !evt.n) return next;
          const status = evt.type === 'step_start' ? 'running' : evt.success === false ? 'failed' : 'done';
          return { ...next, plans: { ...next.plans, [id]: plan.map((s, i) => (i === evt.n! - 1 ? { ...s, status } : s)) } };
        }
        case 'approval':
          return evt.approval ? { ...next, approvals: { ...next.approvals, [evt.approval.id]: evt.approval } } : next;
        case 'job':
          return evt.job ? { ...next, jobs: { ...next.jobs, [evt.job.id]: evt.job } } : next;
        case 'notice':
          return evt.notice ? { ...next, lastNotice: evt.notice } : next;
        case 'chat_started':
          return { ...next, exchanges: [], requests: {}, order: [], drafts: {}, activity: {}, steps: {}, plans: {} };
        default:
          return next;
      }
    }
  }
}

// ------------------------------------------------------------------ what the screen shows

export type TimelineItem =
  | {
      kind: 'exchange'; key: string; id: string; user: string; assistant: string; at: number;
      steps?: Step[]; voice?: boolean; request_id?: string; plan?: PlanStep[];
    }
  | {
      kind: 'request'; key: string; at: number; request: LocalRequest; draft?: string; activity?: string;
      steps?: Step[]; plan?: PlanStep[];
    }
  | { kind: 'job'; key: string; at: number; job: Job };

/** History, the phone's own requests the history does not yet contain, and the background work
 * started in this conversation, in time order. */
export function timeline(state: ChatState): TimelineItem[] {
  // An answered request the history already holds is shown once, as the history's exchange,
  // carrying the request's steps.
  const claimed = new Set<string>();
  const items: TimelineItem[] = state.exchanges.map((e) => {
    const match = state.order.find((id) => {
      const r = state.requests[id];
      return r && !claimed.has(id) && r.state === 'answered' && r.text.trim() === e.user.trim() &&
        e.at * 1000 >= r.sentAt - 120_000;
    });
    if (match) claimed.add(match);
    return { kind: 'exchange', key: `x-${e.id}`, id: e.id, user: e.user, assistant: e.assistant, at: e.at * 1000,
      steps: match ? state.steps[match] : undefined, voice: match ? state.requests[match].input === 'voice' : undefined,
      request_id: match, plan: match ? state.plans[match] : undefined };
  });
  for (const id of state.order) {
    const r = state.requests[id];
    if (!r || claimed.has(id)) continue;
    if (r.state === 'absorbed') continue;    // its words joined another request's turn
    items.push({ kind: 'request', key: `r-${id}`, at: r.sentAt, request: r, draft: state.drafts[id],
      activity: state.activity[id], steps: state.steps[id], plan: state.plans[id] });
  }
  // A job shows from where it started, and only if it started in this conversation: a new
  // chat starts empty, while the server lists phone jobs for three days.
  const start = items.length ? Math.min(...items.map((i) => i.at)) : Infinity;
  for (const job of Object.values(state.jobs)) {
    if (job.started * 1000 < start - 5_000) continue;
    items.push({ kind: 'job', key: `j-${job.id}`, at: job.started * 1000, job });
  }
  return items
    .map((item, n) => ({ item, n }))
    .sort((a, b) => a.item.at - b.item.at || a.n - b.n)       // stable: equal times keep their order
    .map(({ item }) => item);
}

/** A request id the server accepts ([A-Za-z0-9_-]{8,64}), unique enough for one person. */
export function newRequestId(now = Date.now(), rand = Math.random): string {
  let tail = '';
  for (let i = 0; i < 12; i++) tail += Math.floor(rand() * 36).toString(36);
  return `req-${now.toString(36)}-${tail}`;
}

/** Reuse unchanged row objects; a streaming draft only changes its own row's props. */
export function createTimeline() {
  let previous = new Map<string, TimelineItem>();
  return (state: ChatState) => {
    const rows = timeline(state).map((item) => {
      const old = previous.get(item.key);
      const fields = Object.keys(item) as (keyof typeof item)[];
      return old && fields.length === Object.keys(old).length &&
        fields.every((key) => Object.is(item[key], old[key])) ? old : item;
    });
    previous = new Map(rows.map((item) => [item.key, item]));
    return rows;
  };
}
