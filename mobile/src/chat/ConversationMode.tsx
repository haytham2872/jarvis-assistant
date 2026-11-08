// Conversation mode (slice 5): talk to JARVIS without touching the screen. It listens, notices when
// The owner stops talking (voice/conversation.ts), sends, reads the answer aloud in Fenrir's voice,
// then listens again, until he taps End, stays silent for 8 s, or says "that's all". Each turn
// is an ordinary voice message in the phone chat, sent as one he will hear (so JARVIS answers
// briefly). The screen stays awake while it is open; leaving the app ends it, since Expo Go
// cannot record or play in the background.
import { File } from 'expo-file-system';
import { useKeepAwake } from 'expo-keep-awake';
import { useCallback, useEffect, useRef, useState } from 'react';
import { AppState, Modal, Pressable, StyleSheet, Text, useWindowDimensions, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { meterLevel } from '../core/api';
import { useApp } from '../state/AppContext';
import { color, font, space } from '../theme/tokens';
import { Backdrop } from '../ui/Backdrop';
import { Glass } from '../ui/Glass';
import { afterSend, Vad } from '../voice/conversation';
import type { OrbMode } from '../voice/levels';
import { useLiveMic } from '../voice/useLiveMic';
import type { LiveResult } from '../voice/liveVoice';
import { useRecorder } from '../voice/useRecorder';
import { NeuralOrb } from '../orb/NeuralOrb';
import { newRequestId } from './reducer';

type Phase = 'starting' | 'listening' | 'sending' | 'waiting' | 'ended';
const MAX_MISSES = 2;           // recordings in a row the server heard nothing in, before stopping
const TICK_MS = 400;

export function ConversationMode({ onClose }: { onClose: () => void }) {
  useKeepAwake();
  const insets = useSafeAreaInsets();
  const { cfg, chat, speaker, levels, setRecording } = useApp();
  const win = useWindowDimensions();
  // Slice 6: the orb fills the screen's width, leaving room for the words under it.
  const orbSize = Math.round(Math.max(160, Math.min(win.width - space.s5 * 2, win.height * 0.44)));
  const stopRef = useRef<() => void>(() => undefined);
  const rec = useRecorder(useCallback(() => stopRef.current(), []));   // the 2-minute cap ends a turn
  const live = useLiveMic(cfg, useCallback(() => stopRef.current(), []));
  const liveUnavailable = useRef(false);     // the stream failed to start once: use the recorder from then on
  const liveId = useRef<string | null>(null);   // the request id of the live session in progress, else null
  const [phase, setPhase] = useState<Phase>('starting');
  const [note, setNote] = useState('');
  const [turnId, setTurnId] = useState<string | null>(null);
  const [now, setNow] = useState(Date.now());
  const vad = useRef(new Vad());
  const misses = useRef(0);
  const spoke = useRef(false);
  const answeredAt = useRef<number | null>(null);
  const alive = useRef(true);
  const recRef = useRef(rec);
  recRef.current = rec;
  const liveRef = useRef(live);
  liveRef.current = live;
  const lastFeed = useRef(0);

  const end = useCallback(async (why = '') => {
    if (!alive.current) return;
    alive.current = false;
    setPhase('ended');
    setNote(why);
    speaker.stop();
    if (liveId.current) {
      liveId.current = null;
      await liveRef.current.cancel();
    }
    if (recRef.current.active) await recRef.current.cancel();
    setRecording(false);
    levels.push(0);
  }, [speaker, setRecording, levels]);

  const listen = useCallback(async () => {
    if (!alive.current) return;
    speaker.stop();                          // the microphone and his voice never overlap
    vad.current = new Vad();
    setTurnId(null);
    spoke.current = false;
    answeredAt.current = null;
    chat.warmUp();
    if (!liveUnavailable.current) {
      const id = newRequestId();
      const started = await live.start({ requestId: id, mode: 'conversation', spoken: true });
      if (!alive.current) return;
      if (started.ok) {
        liveId.current = id;
        setRecording(true);
        setPhase('listening');
        void started.result.then((res) => onLive(id, res));
        return;
      }
      if (started.why === 'permission') return void end('The microphone is off for Expo Go: turn it on in the iPhone Settings.');
      liveUnavailable.current = true;          // this turn, and the rest, use the recorder
    }
    const err = await rec.start();
    if (err === 'permission') return void end('The microphone is off for Expo Go: turn it on in the iPhone Settings.');
    if (err) return void end('The microphone could not start.');
    setRecording(true);
    setPhase('listening');
  }, [speaker, chat, rec, live, end, setRecording]);   // eslint-disable-line react-hooks/exhaustive-deps

  /** A turn that did not happen (nothing heard): try again, twice at most. */
  const miss = useCallback((line: string | null) => {
    misses.current += 1;
    if (misses.current >= MAX_MISSES) return void end(line ?? 'I could not hear you, so I stopped listening.');
    return void listen();
  }, [end, listen]);

  /** The turn is in the chat: wait for its answer. */
  const submitted = useCallback((requestId: string) => {
    misses.current = 0;
    setTurnId(requestId);
    setPhase('waiting');
  }, []);

  // The server decided: a turn it submitted, nothing heard, or silence; or the socket failed and the WAV goes up.
  const onLive = useCallback(async (id: string, res: LiveResult) => {
    if (!alive.current || liveId.current !== id || res.kind === 'cancelled') return;
    liveId.current = null;
    setPhase('sending');
    const file = await live.finish();
    setRecording(false);
    levels.push(0);
    if (!alive.current) return;
    const drop = () => {
      if (file) try { new File(file.uri).delete(); } catch { /* already gone */ }
    };
    if (res.kind === 'turn') {
      chat.adoptVoiceTurn(id, res.status, true, undefined, file?.uri);
      return void submitted(id);
    }
    if (res.kind === 'no_speech') {
      drop();
      return void end('No one spoke for a while, so I stopped listening.');
    }
    if (res.kind === 'empty') {
      drop();
      return void miss(null);
    }
    // failed: the same request id, so a turn the server did run is never run twice
    if (!file) return void end('That recording could not be saved.');
    const { requestId, line } = await chat.sendVoiceTurn(file.uri, file.ms, true, undefined, id);
    if (!requestId || line) return void miss(line);
    submitted(requestId);
  }, [live, chat, end, miss, submitted, setRecording, levels]);

  const send = useCallback(async () => {
    if (!alive.current) return;
    setPhase('sending');
    if (liveId.current) {                   // live: the server answers with the turn
      void live.end();
      return;
    }
    const r = await rec.finish();
    setRecording(false);
    levels.push(0);
    if (!r) return void end('That recording could not be saved.');
    const { requestId, line } = await chat.sendVoiceTurn(r.uri, r.ms, true);
    if (!requestId || line) return void miss(line);         // too short, or the server heard nothing in it
    submitted(requestId);
  }, [rec, live, chat, end, miss, submitted, setRecording, levels]);
  stopRef.current = () => void send();

  // Start listening on open; end when the app leaves the screen or this closes.
  useEffect(() => {
    alive.current = true;
    void listen();
    const sub = AppState.addEventListener('change', (s) => s !== 'active' && void end());
    return () => {
      sub.remove();
      void end();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Listening: each level sample may end the turn.
  useEffect(() => {
    if (phase !== 'listening') return;
    const t = Date.now();
    if (liveId.current) {                         // live: the orb follows the level, the server ends the turn
      if (live.active) levels.push(meterLevel(live.metering));
      return;
    }
    if (!rec.active) return;
    if (t - lastFeed.current < 80) return;        // one sample per meter update, not per render
    lastFeed.current = t;
    levels.push(meterLevel(rec.metering));
    const ev = vad.current.feed(rec.metering, t);
    if (ev === 'end') void send();
    else if (ev === 'no_speech') void end('No one spoke for a while, so I stopped listening.');
  }, [phase, rec.active, rec.metering, live.active, live.metering, send, end, levels]);

  // Waiting: a clock, so a spoken answer that never starts is not waited for forever.
  useEffect(() => {
    if (phase !== 'waiting') return;
    const t = setInterval(() => setNow(Date.now()), TICK_MS);
    return () => clearInterval(t);
  }, [phase]);

  const req = turnId ? chat.state.requests[turnId] : undefined;
  const source = turnId ? `req:${turnId}` : null;
  const speakerPhase = source && speaker.state.source === source ? speaker.state.phase : 'idle';
  if (speakerPhase !== 'idle') spoke.current = true;
  if (req && (req.state === 'answered' || req.state === 'absorbed') && answeredAt.current === null) answeredAt.current = now;

  useEffect(() => {
    if (phase !== 'waiting' || !turnId) return;
    const next = afterSend({
      state: req?.state, heard: req?.text, speaker: speakerPhase, spoke: spoke.current,
      sinceAnswered: answeredAt.current === null ? 0 : now - answeredAt.current,
    });
    if (next === 'listen') void listen();
    else if (next === 'end') {
      const failed = req?.state && req.state !== 'answered' && req.state !== 'absorbed';
      void end(failed ? 'JARVIS could not answer that one; the conversation stopped.' : '');
    }
  }, [phase, turnId, req?.state, req?.text, speakerPhase, now, listen, end]);

  const orb: OrbMode = phase === 'listening' ? 'listening'
    : speakerPhase === 'speaking' ? 'speaking'
      : phase === 'sending' ? 'transcribing'
        : phase === 'waiting' ? 'thinking' : 'idle';
  const label = phase === 'ended' ? (note ? 'Stopped' : 'Done')
    : phase === 'starting' ? 'Starting…'
      : phase === 'listening' ? ((liveId.current ? !!live.heard : vad.current.speaking) ? 'Listening…' : 'Go ahead')
        : phase === 'sending' ? 'Sending…'
          : speakerPhase === 'speaking' ? 'Speaking' : 'Thinking…';
  const heard = phase === 'listening' && liveId.current ? live.heard : req?.text;
  const answer = req?.answer;

  return (
    <Modal animationType="fade" presentationStyle="fullScreen" onRequestClose={onClose} visible>
      <View style={styles.screen}>
        <Backdrop />
        <View style={[styles.body, { paddingTop: insets.top + space.s6, paddingBottom: insets.bottom + space.s5 }]}>
          <Text style={styles.title}>Conversation</Text>
          <Pressable accessibilityRole="button" style={styles.orbArea}
            accessibilityLabel={speakerPhase === 'speaking' ? 'Stop JARVIS and speak' : label}
            onPress={speakerPhase === 'speaking' ? speaker.stop : undefined}>
            <NeuralOrb mode={orb} bus={levels} size={orbSize} mood={chat.state.mood} haptics />
          </Pressable>
          <Text style={styles.state} accessibilityLiveRegion="polite" testID="conversation-state">{label}</Text>
          <View style={styles.lines}>
            {heard ? <Text style={styles.heard} numberOfLines={3}>“{heard}”</Text> : null}
            {answer && phase !== 'listening' ? <Text style={styles.answer} numberOfLines={6}>{answer}</Text> : null}
            {note ? <Text style={styles.note}>{note}</Text> : null}
          </View>
          <Text style={styles.hint}>
            {phase === 'ended' ? 'Each turn is in the chat.' : 'Pause when you are done; say "that\'s all" to finish. Tap the orb to cut him off.'}
          </Text>
          <Pressable accessibilityRole="button" accessibilityLabel={phase === 'ended' ? 'Close' : 'End the conversation'}
            onPress={() => { void end(); onClose(); }}>
            <Glass tone={phase === 'ended' ? 'accent' : 'amber'} interactive style={styles.endBtn}>
              <Text style={[styles.endText, phase === 'ended' && styles.endTextDone]}>{phase === 'ended' ? 'Close' : 'End'}</Text>
            </Glass>
          </Pressable>
        </View>
      </View>
    </Modal>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: color.bg },
  body: { flex: 1, alignItems: 'center', paddingHorizontal: space.s5, gap: space.s4 },
  title: { color: color.tMid, fontSize: font.label, fontWeight: '700', letterSpacing: 2, textTransform: 'uppercase' },
  orbArea: { flex: 1, minHeight: 220, alignItems: 'center', justifyContent: 'center', alignSelf: 'stretch' },
  state: { color: color.tHi, fontSize: font.lg, fontWeight: '600' },
  lines: { alignSelf: 'stretch', gap: space.s3, minHeight: 120 },
  heard: { color: color.tMid, fontSize: font.body, textAlign: 'center', fontStyle: 'italic' },
  answer: { color: color.tHi, fontSize: font.body, lineHeight: 22, textAlign: 'center' },
  note: { color: color.warn, fontSize: font.label, textAlign: 'center' },
  hint: { color: color.tDim, fontSize: font.label, textAlign: 'center' },
  endBtn: { minWidth: 160, height: 52, borderRadius: 26, alignItems: 'center', justifyContent: 'center' },
  endText: { color: color.tHi, fontSize: font.read, fontWeight: '700' },
  endTextDone: { color: color.bg },
});
