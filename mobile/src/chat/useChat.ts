// The conversation, wired: state + snapshot + live events + sending, with the app's lifecycle.
import { File } from 'expo-file-system';
import { useCallback, useEffect, useMemo, useReducer, useRef } from 'react';
import { AppState } from 'react-native';

import type { FileChip } from '../attach/model';
import { describeFailure, ExplainedError, type ServerConfig } from '../core/api';
import { mark, since } from '../core/perf';
import { eventBatch } from './eventBatch';
import { decideApproval, findRequest, getSnapshot, HttpError, newChat as postNewChat, sendMessage, sendVoice, stopAnswer, warm } from './client';
import { chatReducer, initialChat, newRequestId } from './reducer';
import { EventStream } from './stream';

/** Shorter than this is a slip of the finger, not a message. */
export const MIN_VOICE_MS = 500;

function forget(uri: string) {
  try {
    new File(uri).delete();
  } catch {
    /* already gone */
  }
}

export function explain(e: unknown): string {
  if (e instanceof ExplainedError) return e.message;
  if (e instanceof HttpError) return describeFailure(e.status, e.message);
  return describeFailure(null, e instanceof Error ? e.message : String(e));
}

export type ChatOptions = {
  /** A voice message whose answer was to be read aloud has its final answer. */
  onSpokenAnswer?: (source: string, answer: string) => void;
};

export function useChat(cfg: ServerConfig | null, options: ChatOptions = {}) {
  const [state, dispatch] = useReducer(chatReducer, initialChat);
  const batch = useMemo(() => eventBatch((events) => dispatch({ type: 'events', events })), []);
  const firstWord = useRef(new Set<string>());
  const measuring = useRef(new Set<string>());
  const onSpoken = useRef(options.onSpokenAnswer);
  onSpoken.current = options.onSpokenAnswer;
  // Requests whose answer is to be read aloud. The phone's wish, not the server's record: a
  // snapshot rebuilds requests from the server and must not lose it.
  const wantSpoken = useRef(new Set<string>());
  // Slice 4: the Inbox message a request answers, kept so a retry answers the same one.
  const replyOf = useRef(new Map<string, number>());
  const uploadsOf = useRef(new Map<string, string[]>());     // slice 7: a retry names the same uploads
  const position = useRef({ epoch: '', seq: 0 });
  position.current = { epoch: state.epoch, seq: state.seq };
  const requests = useRef(state.requests);
  requests.current = state.requests;
  const key = cfg ? `${cfg.baseUrl}|${cfg.token}` : '';

  const refresh = useCallback(async () => {
    if (!cfg) return;
    try {
      const snap = await getSnapshot(cfg);
      batch.flush();
      dispatch({ type: 'snapshot', snap });
    } catch {
      /* offline: the stream's reconnect brings the next snapshot */
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  useEffect(() => {
    if (!cfg) return;
    batch.flush();
    firstWord.current.clear();
    measuring.current.clear();
    dispatch({ type: 'reset' });
    const stream = new EventStream(cfg, {
      position: () => position.current,
      onEvent: (evt) => {
        const id = evt.request_id;
        if (__DEV__ && id && evt.type === 'response' && evt.delta && !firstWord.current.has(id)) {
          firstWord.current.add(id);
          since(`message:${id}`, 'message:first-word');
        }
        if (id && evt.type === 'run_finished') {
          firstWord.current.delete(id);
        }
        batch.push(evt);
      },
      onResync: () => void refresh(),
      onStatus: (live) => {
        dispatch({ type: 'connection', value: live ? 'live' : 'offline' });
        if (live) void refresh();      // anything missed while away comes from the records
      },
    });
    stream.start();
    void refresh();
    const sub = AppState.addEventListener('change', (s) => {
      if (s === 'active') {
        stream.kick();
        void refresh();
      }
    });
    return () => {
      sub.remove();
      stream.stop();
      batch.flush();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  // A snapshot can deliver the answer after a reconnect without a run_finished event.
  useEffect(() => {
    if (!__DEV__) return;
    for (const id of measuring.current) {
      const request = state.requests[id];
      if (request?.state === 'answered') {
        since(`message:${id}`, 'message:answer');
        measuring.current.delete(id);
      } else if (request && ['failed', 'interrupted', 'cancelled', 'send_failed'].includes(request.state)) {
        measuring.current.delete(id);
      }
    }
  }, [state.requests]);

  const deliver = useCallback(async (request_id: string, text: string) => {
    if (!cfg) return;
    try {
      dispatch({ type: 'sent', status: await sendMessage(cfg, request_id, text, 'text', replyOf.current.get(request_id),
        uploadsOf.current.get(request_id)) });
    } catch (e) {
      // The answer was lost, not necessarily the message: ask before calling it failed.
      try {
        const found = await findRequest(cfg, request_id);
        if (found) {
          dispatch({ type: 'sent', status: found });
          return;
        }
      } catch {
        /* still unreachable: report the original failure */
      }
      dispatch({ type: 'send_failed', request_id, error: explain(e) });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  // Only the final answer is read aloud: the request reaches 'answered' from run_finished or
  // a snapshot, never from the draft that streamed. Each answer is announced once.
  useEffect(() => {
    for (const id of wantSpoken.current) {
      const r = state.requests[id];
      if (!r) continue;
      if (r.state === 'answered' && r.answer) {
        wantSpoken.current.delete(id);
        onSpoken.current?.(`req:${id}`, r.answer);
      } else if (r.state === 'failed' || r.state === 'interrupted' || r.state === 'cancelled') {
        wantSpoken.current.delete(id);
      }
    }
  }, [state.requests, state.order]);

  /** Uploads a recording; returns a line for the owner when nothing was heard, else null. */
  const deliverVoice = useCallback(async (request_id: string, uri: string, spoken = false): Promise<string | null> => {
    if (!cfg) return null;
    try {
      dispatch({ type: 'sent', status: (await sendVoice(cfg, request_id, uri, spoken, replyOf.current.get(request_id))).status });
      forget(uri);
      return null;
    } catch (e) {
      if (e instanceof HttpError && e.status === 422) {
        dispatch({ type: 'heard_nothing', request_id });     // no message exists: nothing to show
        forget(uri);
        return e.message;
      }
      try {
        const found = await findRequest(cfg, request_id);
        if (found) {
          dispatch({ type: 'sent', status: found });
          forget(uri);
          return null;
        }
      } catch {
        /* still unreachable */
      }
      dispatch({ type: 'send_failed', request_id, error: explain(e) });   // the file stays for Retry
      return null;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  /** Uploads a recording as a message; `requestId` names it (null when too short to send),
   * `line` is for the owner when nothing was heard. */
  const sendVoiceTurn = useCallback(async (uri: string, ms: number, spoken = false, replyTo?: number):
    Promise<{ requestId: string | null; line: string | null }> => {
    if (ms < MIN_VOICE_MS) {
      forget(uri);
      return { requestId: null, line: null };
    }
    const request_id = newRequestId();
    mark(`message:${request_id}`);
    if (__DEV__) measuring.current.add(request_id);
    if (replyTo) replyOf.current.set(request_id, replyTo);
    if (spoken) wantSpoken.current.add(request_id);
    dispatch({ type: 'send_voice', request_id, at: Date.now(), audio: uri });
    const line = await deliverVoice(request_id, uri, spoken);
    if (line) wantSpoken.current.delete(request_id);      // nothing was heard: no answer coming
    return { requestId: request_id, line };
  }, [deliverVoice]);

  const sendRecording = useCallback(async (uri: string, ms: number, spoken = false, replyTo?: number): Promise<string | null> =>
    (await sendVoiceTurn(uri, ms, spoken, replyTo)).line, [sendVoiceTurn]);

  /** Recording started: the server loads its transcriber meanwhile. */
  const warmUp = useCallback(() => {
    if (cfg) void warm(cfg);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  /** `attach` (slice 7): files already uploaded (their ids) and what to show for them. */
  const send = useCallback(async (text: string, replyTo?: number, attach?: { uploads: string[]; files: FileChip[] }) => {
    const clean = text.trim();
    if (!clean && !attach?.uploads.length) return;
    const request_id = newRequestId();
    mark(`message:${request_id}`);
    if (__DEV__) measuring.current.add(request_id);
    if (replyTo) replyOf.current.set(request_id, replyTo);
    if (attach?.uploads.length) uploadsOf.current.set(request_id, attach.uploads);
    dispatch({ type: 'send', request_id, text: clean, at: Date.now(), files: attach?.files });
    await deliver(request_id, clean);
  }, [deliver]);

  /** Same request id, so a message that did land is never run twice. */
  const retry = useCallback(async (request_id: string) => {
    const r = requests.current[request_id];
    if (!r) return;
    mark(`message:${request_id}`);
    if (__DEV__) measuring.current.add(request_id);
    firstWord.current.delete(request_id);
    if (r.input === 'voice' && r.audio) {
      dispatch({ type: 'send_voice', request_id, at: r.sentAt, audio: r.audio });
      await deliverVoice(request_id, r.audio, wantSpoken.current.has(request_id));
      return;
    }
    if (!r.text && !uploadsOf.current.has(request_id)) return;
    dispatch({ type: 'send', request_id, text: r.text, at: r.sentAt, files: r.files });
    await deliver(request_id, r.text);
  }, [deliver, deliverVoice]);

  const startNewChat = useCallback(async (): Promise<string> => {
    if (!cfg) return 'Not paired.';
    try {
      const { message } = await postNewChat(cfg);
      await refresh();
      return message;
    } catch (e) {
      return explain(e);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, refresh]);

  /** The Stop button. The run then ends as interrupted through the usual events. */
  const stop = useCallback(async (): Promise<void> => {
    if (!cfg) return;
    try { await stopAnswer(cfg); } catch { /* the next refresh shows what happened */ }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  /** Approve or deny a request; returns what happened, in words. */
  const decide = useCallback(async (id: number, approve: boolean): Promise<string> => {
    if (!cfg) return 'Not paired.';
    try {
      const { text, approval } = await decideApproval(cfg, id, approve);
      if (approval) dispatch({ type: 'approval', approval });
      return text;
    } catch (e) {
      return explain(e);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  /** A slash command's reply, shown in the conversation as JARVIS's message. */
  const showCommand = useCallback((text: string, reply: string) => {
    dispatch({ type: 'command', request_id: newRequestId(), text, reply, at: Date.now() });
  }, []);

  return { state, send, sendRecording, sendVoiceTurn, warmUp, retry, startNewChat, refresh, decide, stop, showCommand };
}
