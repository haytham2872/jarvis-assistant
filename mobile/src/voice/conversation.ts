// Conversation mode (slice 5), as pure decisions: when the owner has finished speaking, and what to
// do once an answer arrives. The screen (chat/ConversationMode.tsx) does the recording and playing.
//
// End of speech is decided from the microphone level iOS reports every 100 ms (dB, about -160 to
// 0). A fixed threshold fails in a car or a café, so the first half second measures the room and
// speech is counted above that floor: talk counts once 250 ms of it have been heard, and the turn
// ends after 1.3 s back near the floor. Nothing heard for 8 s ends the conversation.

export type VadEvent = 'end' | 'no_speech' | null;

export const VAD = {
  calibrateMs: 500,
  speechAboveFloorDb: 14,
  quietAboveFloorDb: 7,
  voicedMsToCount: 250,
  silenceMsToEnd: 1300,
  noSpeechMs: 8000,
  floorMinDb: -70,
  floorMaxDb: -30,
} as const;

export class Vad {
  private start: number | null = null;
  private calib: number[] = [];
  private floor: number | null = null;
  private voicedMs = 0;
  private heard = false;
  private quietSince: number | null = null;
  private last: number | null = null;

  constructor(private cfg: typeof VAD = VAD) {}

  /** Has he said something yet (for the screen: "Listening" vs "Go ahead")? */
  get speaking(): boolean {
    return this.heard;
  }

  /** One level sample at time `t` (ms). Returns 'end' when he has finished, 'no_speech' when he
   * never started, otherwise null. */
  feed(db: number | null | undefined, t: number): VadEvent {
    if (this.start === null) this.start = t;
    const step = this.last === null ? 0 : Math.max(0, Math.min(500, t - this.last));
    this.last = t;
    const level = typeof db === 'number' && Number.isFinite(db) ? db : -160;

    if (this.floor === null) {
      this.calib.push(level);
      if (t - this.start < this.cfg.calibrateMs) return null;
      const sorted = [...this.calib].sort((a, b) => a - b);
      const median = sorted[Math.floor(sorted.length / 2)];
      this.floor = Math.max(this.cfg.floorMinDb, Math.min(this.cfg.floorMaxDb, median));
      return null;
    }

    if (level > this.floor + this.cfg.speechAboveFloorDb) {
      this.voicedMs += step;
      if (this.voicedMs >= this.cfg.voicedMsToCount) this.heard = true;
      this.quietSince = null;
    } else if (this.heard) {
      if (level < this.floor + this.cfg.quietAboveFloorDb) {
        if (this.quietSince === null) this.quietSince = t;
        if (t - this.quietSince >= this.cfg.silenceMsToEnd) return 'end';
      } else {
        this.quietSince = null;          // between the two thresholds: still trailing off
      }
    }
    if (!this.heard && t - this.start >= this.cfg.noSpeechMs) return 'no_speech';
    return null;
  }
}

/** After a turn was sent: keep waiting, listen again, or end the conversation. */
export type AfterSend = 'wait' | 'listen' | 'end';

export type TurnView = {
  /** The request's state (useChat), or undefined while it is still being uploaded. */
  state?: string;
  /** What the server heard, once it has. */
  heard?: string | null;
  /** The speaker is busy with this request's answer: 'loading' or 'speaking'; 'idle' otherwise. */
  speaker: 'idle' | 'loading' | 'speaking';
  /** The speaker has started on this answer at some point. */
  spoke: boolean;
  /** Milliseconds since the request was answered (0 until it is). */
  sinceAnswered: number;
};

/** A spoken answer that never starts playing (the voice failed) is not waited for forever. */
export const SPEECH_START_GRACE_MS = 6000;

const FAILED = new Set(['failed', 'interrupted', 'cancelled', 'send_failed', 'unknown']);

/** "That's all", "c'est tout": he is done, after this answer. */
export function isGoodbye(heard: string | null | undefined): boolean {
  const t = (heard ?? '').toLowerCase().replace(/[.!?,]/g, ' ').replace(/\s+/g, ' ').trim();
  return /^(?:(?:ok(?:ay)?|thanks?|thank you|merci)\s+)*(?:that's all|that is all|stop|goodbye|bye|c'est tout|arrête|au revoir)(?:\s+(?:for now|thanks?|thank you|jarvis|merci))*$/.test(t);
}

export function afterSend(v: TurnView): AfterSend {
  if (v.state && FAILED.has(v.state)) return 'end';
  if (v.state !== 'answered' && v.state !== 'absorbed') return 'wait';
  if (v.speaker !== 'idle') return 'wait';
  if (!v.spoke && v.sinceAnswered < SPEECH_START_GRACE_MS) return 'wait';
  return isGoodbye(v.heard) ? 'end' : 'listen';
}
