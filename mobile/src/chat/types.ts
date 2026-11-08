// The /mobile/v1 conversation contract (backend/mobile/conversation.py, routes.py).

export type RequestState =
  | 'queued' | 'running' | 'answered' | 'failed' | 'interrupted' | 'cancelled' | 'absorbed' | 'unknown';

/** GET /requests/{id}, POST /messages, and each entry of the snapshot's `requests`. */
export type RequestStatus = {
  request_id: string;
  state: RequestState;
  input: 'text' | 'voice';
  text: string | null;
  run_id: string | null;
  answer: string | null;
  received_at: number;
  finished_at: number | null;
  steered: boolean;
  /** The tools the turn used, from the server's receipts (durable). */
  tools?: ToolUse[];
  /** Slice 7: files sent with the message that could not be read, in words. */
  problems?: string[];
};

export type ToolUse = { tool: string; outcome: string; detail: string };

export type Exchange = { id: string; session_id: string; at: number; user: string; assistant: string };

export type Snapshot = {
  schema_version: 1;
  epoch: string;
  seq: number;
  exchanges: Exchange[];
  requests: RequestStatus[];
  running: { run_id: string } | null;
  /** M4: approval requests waiting on him (any origin), and work he started from the phone. */
  approvals?: Approval[];
  jobs?: Job[];
  /** Slice 6: JARVIS's mood, which tints the orb. */
  mood?: Mood | null;
};

/** JARVIS's mood (backend/brain/mood.py): a tone and how strong it is, 0..1. */
export type Mood = { tone: string; intensity: number };

export type ApprovalState = 'pending' | 'executing' | 'executed' | 'failed' | 'uncertain' | 'denied' | 'expired';

/** An action JARVIS needs his OK for (backend/storage/approvals.py), as the phone sees it. */
export type Approval = {
  id: number;
  state: ApprovalState;
  preview: string;
  why: string;
  context: string;
  created_at: number;
  expires_at: number;
  decided_at: number | null;
  decided_how: string | null;
  outcome: string;
};

export type JobStatus = 'queued' | 'running' | 'done' | 'failed' | 'cancelled';

/** Background work started from the phone (backend/brain/background.py). */
export type Job = {
  id: number;
  status: JobStatus;
  kind: string;
  description: string;
  started: number;
  finished: number | null;
  summary: string;
  result: string;
  note_file: string;
  /** A finished job's conclusion (backend/brain/conclusion.py), shown as JARVIS's message. */
  conclusion?: string;
};

/** One event from WS /events. */
export type HubEvent = {
  type: string;
  seq: number;
  epoch: string;
  at: number;
  request_id?: string | null;
  run_id?: string | null;
  delta?: string;
  answer?: string | null;
  outcome?: string;
  error?: string | null;
  steers_run?: string | null;
  tool?: string;
  detail?: string;
  approval?: Approval;
  job?: Job;
  /** plan / plan_update: the turn's steps; step_start / step_done: one of them, 1-based. */
  steps?: PlanStep[];
  n?: number;
  label?: string;
  success?: boolean;
  reason?: string | null;
  /** notice (slice 4): a message JARVIS just sent on his own. */
  notice?: Notice;
  /** run_finished (slice 6): his mood once the turn is done. */
  mood?: Mood | null;
};

export type PlanStepStatus = 'pending' | 'running' | 'done' | 'failed';

export type PlanStep = { id: number; label: string; tool: string | null; status: PlanStepStatus };

export type ChatSummary = {
  session_id: string; started_at: number; last_at: number; exchanges: number; first_user_text: string;
};

/** GET /activity (backend/brain/activity.py activity_view): the sections of /activity. */
export type ActivitySection = { title: string; items: string[]; empty: string };
export type ActivityView = { as_of: number; paused: string | null; sections: ActivitySection[] };

/** GET /today (backend/mobile/today.py): the owner's day. Times are Unix seconds. */
export type Outcome = 'done' | 'partly' | 'skipped' | 'moved' | 'unknown';
export type TodayBlock = {
  uid: string; kind: string; title: string; start: number; end: number;
  /** "moved": he moved its calendar event himself; "failed": it never reached the calendar. */
  status: string;
  outcome: { outcome: Outcome; reason: string; actor: string; at: number | null } | null;
  started: boolean;
  /** A pending request to move it (a skip's new slot, or a meeting landing on it). */
  offer: { id: number; label: string; why: string } | null;
};
export type TodayEvent = { title: string; start: number; end: number; all_day: boolean; where: string };
export type TodayItem = { at: number; text: string; repeat: string; skill?: string };
export type WeekDay = { date: string; day: string; planned: number } & Record<Outcome, number>;
export type TodayView = {
  date: string; label: string; now: number;
  blocks: TodayBlock[];
  plan_error?: string;
  yesterday: { date: string; label: string; blocks: TodayBlock[] };
  checkin: { enabled: boolean; time: string; waiting: boolean; sent: boolean };
  reminders: TodayItem[];
  tasks: TodayItem[];
  week: WeekDay[];
  events: TodayEvent[] | null;
  events_note: string;
};
/** `note` is what the card does not already show (an automatic move, no free slot...); `text` is all of it. */
export type TapResult = { text: string; note: string; asked: number[]; today: TodayView };

/** GET /portfolio (backend/mobile/ledger.py): the ledger. Figures are exact decimal strings
 * (percentages rounded to 2 places); null where the ledger has no value. */
export type Trade = { id: number; side: string; shares: string; price: string | null; currency: string; traded_on: string };
export type Position = {
  symbol: string; name: string; currency: string; exchange: string;
  shares: string; average_cost: string; cost_basis: string;
  close: string | null; close_date: string | null; close_source: string; close_url: string;
  prev_close: string | null; day_change_pct: string | null;
  value: string | null; pnl: string | null; pnl_pct: string | null;
  value_base: string | null; pnl_base: string | null; weight_pct: string | null;
  /** The currency value_base and pnl_base are in (the server's JARVIS_BASE_CURRENCY). */
  base_currency?: string;
  realised: string;
  /** The feed reports a split the ledger lacks: figures stay at the last close before it. */
  held_for_split: boolean;
  closes: [string, string][];
  trades: Trade[];
};
export type Condition = {
  id: number; kind: 'price_below' | 'price_above' | 'dated_event' | 'metric'; direction: 'wrong_if' | 'right_if';
  level: string | null; effective_level: string | null; criterion: string; due_date: string; state: string;
  /** Percent of the last close still to go before a price level; zero or less once reached. */
  distance_pct: string | null; linked: string;
};
export type Thesis = {
  id: number; symbol: string; why: string; source: 'owner' | 'my analysis'; horizon: string; status: string;
  since: string; conditions: Condition[]; last_event: { kind: string; reason: string; date: string } | null; note: string;
};
export type PendingSplit = {
  symbol: string; split_date: string; ratio: string; shares_before: string | null; shares_after: string | null;
  source: string; approval: { id: number; expires_at: number } | null;
};
export type PortfolioView = {
  as_of: number;
  /** The currency every total is in (the server's JARVIS_BASE_CURRENCY). */
  base_currency?: string;
  positions: Position[];
  watching: { symbol: string; name: string; currency: string; close: string | null; close_date: string | null }[];
  closed: { symbol: string; name: string; currency: string; realised: string }[];
  theses: Thesis[];
  splits: PendingSplit[];
  history: { date: string; value_base: string | null; profit_base: string | null }[];
  total: { value_base: string | null; pnl_base: string | null; close_date: string | null; complete: boolean; note: string } | null;
  day: { date: string; change_base: string | null; change_pct: string | null } | null;
  error?: string;
};

// Next plan, slice 3: the living portfolio. Drawing data from backend/mobile/charts.py: prices are
// split-adjusted (today's shares), and trades and levels are moved into the same shares.
export type ChartRange = '1M' | '6M' | '1Y' | 'ALL';
export type TotalHistory = {
  range: ChartRange; currency: string; gaps: number; stale: boolean; note?: string; first_trade?: string;
  points: { day: string; value_base: string | null; cost_base: string | null }[];
};
export type ChartTrade = { id: number; day: string; side: 'buy' | 'sell' | 'opening'; shares: string; price: string; fees: string };
export type ChartLevel = {
  id: number; thesis_id: number; kind: 'price_below' | 'price_above'; direction: 'wrong_if' | 'right_if';
  level: string; set_level: string; state: string; criterion: string; distance_pct: string | null;
};
export type ChartDated = { id: number; thesis_id: number; direction: 'wrong_if' | 'right_if'; day: string; criterion: string; state: string };
export type ChartForecast = {
  id: number; claim: string; p: number | null; state: string; outcome: number | boolean | null;
  made: string; due: string | null; settled: string | null;
};
export type HoldingView = {
  symbol: string; name: string; currency: string; range: ChartRange; start: string; stale: boolean;
  history: [string, string][]; splits: { day: string; ratio: string }[];
  trades: ChartTrade[]; average_cost: string | null; shares: string | null;
  levels: ChartLevel[]; dated: ChartDated[]; forecasts: ChartForecast[];
  position: Position | null; watching: PortfolioView['watching'][number] | null;
  closed: PortfolioView['closed'][number] | null; theses: Thesis[]; split: PendingSplit | null;
};
export type ProposalResult = { text: string; new: boolean; watch_id?: string | null };

// slice 4: the week (next plan). UTC instants; day and clock fields are in the owner's zone.
export type WeekBlock = { uid: string; kind: string; title: string; start: number; end: number;
  outcome: Outcome | null; status: string };
export type WeekEvent = { id: string; title: string; start: number; end: number; all_day: boolean };
export type WeekView = {
  week: string; start: string; now: number; plan_id: number | null; notes: string[];
  days: { date: string; label: string; blocks: WeekBlock[]; events: WeekEvent[]; events_note: string;
    counts: { planned: number; done: number; partly: number; skipped: number } }[];
  rules: { kinds: { kind: string; minutes: number; addable: boolean; start: string; end: string }[];
    hours: { start: string; end: string }; buffer: number };
  draft: { plan_id: number; week: string; blocks: WeekBlock[] } | null;
};
export type WeekContext = { view_start: string; days: number; plan_id: number | null };
export type WeekChange = { text: string; warning: string; week: WeekView };

// Slice 4: the Inbox -- every message JARVIS sent the owner on his own (brain/notices.py).
export type NoticeKind =
  | 'reminder' | 'brief' | 'check' | 'job' | 'research' | 'watch' | 'forecast' | 'goal' | 'checkin'
  | 'review' | 'plan' | 'portfolio' | 'profile' | 'pause' | 'system' | 'notice';
export type Notice = {
  id: number; ts: number; kind: NoticeKind | string; text: string;
  /** What became of the Discord copy: null = not sent there, true = sent, false = the send failed. */
  discord: boolean | null; read: boolean;
  /** The owner's thumb on a research finding (only research messages carry it). */
  vote?: 'up' | 'down' | null;
};
/** Where JARVIS's own messages go: Discord only (the default), both, or the phone only. */
export type NoticeRoute = 'discord' | 'both' | 'phone';
/** Pushes from the server: 'confirmed' once this phone showed the test notification. */
export type PushState = 'confirmed' | 'waiting' | 'unknown';
export type NoticeSettings = { route: NoticeRoute; push: PushState };
export type InboxView = NoticeSettings & { notices: Notice[]; unread: number };
export type UpcomingItem = {
  id: string; kind: NoticeKind | string; at: number; title: string; body: string; screen: 'inbox' | 'today';
};

// Next plan slice 2: goals.
export type GoalState = 'active' | 'waiting' | 'paused' | 'done' | 'partial' | 'cancelled' | 'failed' | 'proposed';
export type GoalWait = { wait_id: string; kind: string; label: string; milestone_id: string };
export type GoalCard = {
  goal_id: string; title: string; state: GoalState; created_at: number; deadline_at: number | null;
  budget_usd: number; spent_usd: number | null; milestones: { done: number; total: number };
  now: string | null; waiting: GoalWait[]; outcome: { reason?: string } | null;
};
export type GoalProposal = { goal_id: string; title: string; criteria: string[]; budget_usd: number; deadline: string | null; created_at: number };
export type GoalsView = { enabled: boolean; reason: string; proposals: GoalProposal[]; goals: GoalCard[]; notes: string[] };
export type GoalMilestone = { id: string; title: string; state: string; depends: string[];
  outputs: { name: string; revision: number }[]; summary: string };
export type GoalEvidence = { id: string; title: string; url: string; tool: string; excerpt: string; truncated: boolean };
export type GoalFile = { name: string; revision: number; size: number; text: string; truncated: boolean };
export type GoalDetail = Omit<GoalCard, 'milestones'> & {
  enabled: boolean; reason: string; notes: string[]; milestones: GoalMilestone[];
  session: { state: string; started_at: number; steps: string[] } | null;
  questions: { wait_id: string; text: string }[]; charges: { spent_usd: number | null; reserved_usd: number | null };
  files: { name: string; revision: number; size: number }[]; result: string | null; result_truncated: boolean; evidence: GoalEvidence[];
};
export type HubView = { goals?: { active: number; questions: number; proposals: number; enabled: boolean };
  record?: { settled: number; brier: number | null; early?: boolean };
  overnight?: { since: number; count: number; complete: boolean };
  mind?: { count: number; total: number; complete: boolean }; notes?: string[] };

// slice 7: the mind map. Unknown creation dates explicitly use the file's modification time.
export type MindNode = { id: string; folder: string; name: string; title: string; size: number;
  created: number; updated: number; created_source: 'recorded' | 'filename' | 'modified';
  link: NoteLink; state?: 'done' | 'partial' };
export type MindEdge = { a: string; b: string; kind: 'link' | 'mention' };
export type MindView = { nodes: MindNode[]; edges: MindEdge[]; total: number; truncated: boolean;
  notes: string[]; complete: boolean };

// slice 6: while you slept. Times are epochs; date_only means no clock was recorded.
export type NoteLink = { folder: string; name: string };
export type VaultNote = NoteLink & { text: string; modified: number; truncated: boolean };
export type OvernightKind = 'research' | 'rumination' | 'watch' | 'night_task' | 'timed_check' | 'forecast' | 'goal' | 'skills' | 'job' | 'letter';
export type OvernightItem = { id: string; at: number; kind: OvernightKind; title: string; detail: string;
  link: NoteLink | null; ok: boolean | null; date_only?: boolean; goal_id?: string;
  event?: 'milestone' | 'question' | 'delivered'; forecast_id?: number };
export type OvernightView = { as_of: number; since: number; items: OvernightItem[]; notes: string[]; complete: boolean };

// slice 5: the track record. Scores, bands and the early-record threshold come from Discord's engine.
export type RecordSummary = { n: number; brier?: number; se?: number | null; base_rate?: number; skill?: number | null };
export type RecordBand = { lo: number; hi: number; n: number; hits: number; mean_p: number; interval: [number, number] };
export type RecordCall = { id: number; claim: string; criteria: string; domain: string; raw_p: number; stated_p: number;
  outcome: number | null; resolved_at: number | null; resolver: string; resolution_quote: string; check_query: string;
  resolve_by: number; method: string; created_at: number; source_quote: string };
export type RecordView = { available: boolean; summary: RecordSummary; summary_gut: RecordSummary;
  bands: RecordBand[]; bands_gut: RecordBand[];
  by_month: { month: string; n: number; brier: number; brier_gut: number }[];
  by_domain: { domain: string; n: number; brier: number; brier_gut: number }[];
  settled: RecordCall[]; open: RecordCall[]; void_count: number; withdrawn_count: number;
  early: boolean; threshold: number; text: string; caution: string; notes: string[] };

// The portrait: how JARVIS sees the owner now, and the lines that faded out of it (dates are YYYY-MM-DD).
export type PortraitEntry = { text: string; as_of: string | null };
export type PortraitView = { migrated: boolean; sections: { title: string; entries: PortraitEntry[] }[];
  faded: { faded_on: string; entries: PortraitEntry[] }[]; fading_days: number };
