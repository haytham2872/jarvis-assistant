// The composer (docs/archive/MOBILE-APP-PLAN.md M2): type, or tap the mic, speak, tap send. The recording
// goes to the server, which transcribes it and runs it as a message; the heard text then
// replaces the placeholder bubble. Slice 1: a Liquid Glass capsule floating over the chat, the
// send and mic buttons in JARVIS blue, amber while recording.
import { SymbolView, type SymbolViewProps } from 'expo-symbols';
import { useCallback, useEffect, useRef, useState } from 'react';
import { Linking, Pressable, StyleSheet, Text, TextInput, View } from 'react-native';

import { AttachTray } from '../attach/AttachTray';
import { sendable, type Pending } from '../attach/model';
import { meterLevel, type ServerConfig } from '../core/api';
import { color, font, mono, space } from '../theme/tokens';
import { Glass } from '../ui/Glass';
import type { LevelBus } from '../voice/levels';
import { useRecorder } from '../voice/useRecorder';
import { runCommand } from './client';
import { CommandSheet, commandError } from './CommandSheet';

type Notice = { text: string; settings?: boolean };

type Props = {
  enabled: boolean;
  draft: string;
  setDraft: (text: string) => void;
  onSendText: () => void;
  /** Recording started: warm the transcriber. */
  onRecordStart: () => void;
  /** Recording done: a line for the owner back (nothing heard), or null. */
  onRecording: (uri: string, ms: number) => Promise<string | null>;
  /** The orb's input: the real microphone level while recording. */
  levels?: LevelBus;
  /** Recording started or stopped (the orb's listening state). */
  onActiveChange?: (active: boolean) => void;
  /** Start recording by itself (the /talk link); onAutoStarted fires once it has tried. */
  autoStart?: boolean;
  onAutoStarted?: () => void;
  /** Slice 5: open conversation mode (hands-free, back and forth). */
  onConversation?: () => void;
  /** Slice 7: files waiting to go with the message, and the paperclip that adds more. */
  attachments?: Pending[];
  onAttach?: () => void;
  onRemoveAttachment?: (id: string) => void;
  onRetryAttachment?: (id: string) => void;
  /** While JARVIS is writing an answer: stop it. */
  onStop?: () => void;
  /** The command button and "/..." messages: the server to run them on, and where the reply goes. */
  cfg?: ServerConfig | null;
  onCommandReply?: (text: string, reply: string) => void;
};

const NOTICE_MS = 6_000;

export function elapsed(ms: number): string {
  const s = Math.floor(Math.max(0, ms) / 1000);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

export function Composer(p: Props) {
  const stopRef = useRef<() => void>(() => undefined);
  const rec = useRecorder(useCallback(() => stopRef.current(), []));   // the 2-minute cap sends it
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<Notice | null>(null);

  const { levels, onActiveChange } = p;
  useEffect(() => {
    levels?.push(rec.active ? meterLevel(rec.metering) : 0);
  }, [levels, rec.active, rec.metering]);
  useEffect(() => {
    onActiveChange?.(rec.active);
  }, [onActiveChange, rec.active]);

  useEffect(() => {
    if (!notice) return;
    const t = setTimeout(() => setNotice(null), NOTICE_MS);
    return () => clearTimeout(t);
  }, [notice]);

  const begin = async () => {
    if (busy || !p.enabled) return;
    setBusy(true);
    setNotice(null);
    p.onRecordStart();
    const err = await rec.start();
    setBusy(false);
    if (err === 'permission') setNotice({ text: 'The microphone is off for Expo Go.', settings: true });
    else if (err === 'failed') setNotice({ text: 'The microphone could not start. Try again.' });
  };

  const { autoStart, onAutoStarted } = p;
  useEffect(() => {
    if (!autoStart) return;
    onAutoStarted?.();
    if (!rec.active) void begin();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoStart]);

  const stop = async () => {
    if (busy) return;
    setBusy(true);
    const r = await rec.finish();
    setBusy(false);
    if (!r) {
      setNotice({ text: 'That recording could not be saved. Try again.' });
      return;
    }
    const line = await p.onRecording(r.uri, r.ms);
    if (line) setNotice({ text: line });
  };
  stopRef.current = () => void stop();

  const cancel = async () => {
    if (busy) return;
    setBusy(true);
    await rec.cancel();
    setBusy(false);
  };

  const [sheet, setSheet] = useState(false);
  const { cfg, onCommandReply } = p;
  /** Runs a slash command; true when it was one (a 400 means the server did not take it as one). */
  const command = async (text: string): Promise<'ran' | 'failed' | 'plain'> => {
    if (!cfg) return 'plain';
    try {
      onCommandReply?.(text, await runCommand(cfg, text));
      return 'ran';
    } catch (e) {
      if ((e as { status?: unknown })?.status === 400) return 'plain';
      setNotice({ text: commandError(e) });
      return 'failed';
    }
  };
  const send = async () => {
    const text = p.draft.trim();
    if (text.startsWith('/') && cfg && !sendable(p.attachments ?? []).ready.length) {
      const r = await command(text);
      if (r === 'ran') p.setDraft('');
      if (r !== 'plain') return;
    }
    p.onSendText();
  };
  const runFromSheet = async (text: string) => {
    setSheet(false);
    await command(text);
  };

  const hasText = !!p.draft.trim();
  const files = sendable(p.attachments ?? []);
  const canSend = hasText || files.ready.length > 0;
  const full = (p.attachments?.length ?? 0) >= 5;
  return (
    <View style={styles.wrap}>
      {notice ? (
        <View style={styles.notice} accessibilityLiveRegion="polite">
          <Text style={styles.noticeText}>{notice.text}</Text>
          {notice.settings ? (
            <Pressable accessibilityRole="button" onPress={() => void Linking.openSettings()} hitSlop={8}>
              <Text style={styles.noticeAction}>Open Settings</Text>
            </Pressable>
          ) : null}
        </View>
      ) : null}

      {!rec.active && p.attachments?.length ? (
        <AttachTray items={p.attachments} onRemove={(id) => p.onRemoveAttachment?.(id)}
          onRetry={(id) => p.onRetryAttachment?.(id)} />
      ) : null}

      {rec.active ? (
        <View style={styles.row}>
          <RoundBtn symbol="xmark" label="Cancel recording" onPress={() => void cancel()} tone="plain" />
          <Glass tone="amber" style={styles.capsule} testID="recording-bar">
            <View style={styles.recInner} accessibilityLabel={`Recording, ${elapsed(rec.ms)}`}>
              <View style={styles.recDot} />
              <Text style={styles.recTime}>{elapsed(rec.ms)}</Text>
              <Meter level={meterLevel(rec.metering)} />
            </View>
          </Glass>
          <RoundBtn symbol="arrow.up" label="Send recording" onPress={() => void stop()} tone="accent" />
        </View>
      ) : (
        <View style={styles.row}>
          {cfg ? (
            <RoundBtn symbol="command" label="Commands" onPress={() => setSheet(true)} tone="plain" disabled={!p.enabled} />
          ) : null}
          {p.onAttach ? (
            <RoundBtn symbol="paperclip" label="Attach a photo or file" onPress={p.onAttach} tone="plain"
              disabled={!p.enabled || full} />
          ) : null}
          <Glass style={styles.capsule}>
            <TextInput
              style={styles.input}
              value={p.draft}
              onChangeText={p.setDraft}
              placeholder={p.enabled ? 'Message JARVIS' : 'Pair first'}
              placeholderTextColor={color.tDim}
              multiline
              editable={p.enabled}
              accessibilityLabel="Message"
              selectionColor={color.stIdle}
            />
          </Glass>
          {canSend ? (
            <RoundBtn symbol="arrow.up" label={files.busy ? 'Send (waiting for a file to upload)' : 'Send'}
              onPress={() => void send()} tone="accent" disabled={!p.enabled || files.busy} />
          ) : p.onStop ? (
            <RoundBtn symbol="stop.fill" label="Stop JARVIS" onPress={p.onStop} tone="accent" disabled={!p.enabled} />
          ) : (
            <>
              {p.onConversation ? (
                <RoundBtn symbol="waveform" label="Start a conversation" onPress={p.onConversation} tone="plain"
                  disabled={!p.enabled || busy} />
              ) : null}
              <RoundBtn symbol="mic.fill" label="Record a voice message" onPress={() => void begin()} tone="accent"
                disabled={!p.enabled || busy} />
            </>
          )}
        </View>
      )}
      {cfg ? <CommandSheet visible={sheet} cfg={cfg} onClose={() => setSheet(false)} onRun={(t) => void runFromSheet(t)} /> : null}
    </View>
  );
}

/** A bar that follows the input level: proof the phone hears him. */
function Meter({ level }: { level: number }) {
  return (
    <View style={styles.meter}>
      <View style={[styles.meterFill, { width: `${Math.round(level * 100)}%` }]} />
    </View>
  );
}

function RoundBtn(p: {
  symbol: SymbolViewProps['name']; label: string; onPress: () => void; tone: 'accent' | 'plain'; disabled?: boolean;
}) {
  const accent = p.tone === 'accent';
  return (
    <Pressable accessibilityRole="button" accessibilityLabel={p.label} onPress={p.onPress} disabled={p.disabled}
      hitSlop={6} style={({ pressed }) => [p.disabled && styles.off, pressed && styles.pressed]}>
      <Glass tone={accent ? 'accent' : 'clear'} interactive style={styles.round}>
        <SymbolView name={p.symbol} size={18} weight="semibold" tintColor={accent ? color.bg : color.tHi}
          fallback={<Text style={[styles.fallback, { color: accent ? color.bg : color.tHi }]}>{p.label[0]}</Text>} />
      </Glass>
    </Pressable>
  );
}

const ROW_H = 44;

const styles = StyleSheet.create({
  wrap: { paddingHorizontal: space.s3, paddingTop: space.s2, paddingBottom: space.s2, gap: space.s2 },
  row: { flexDirection: 'row', alignItems: 'flex-end', gap: space.s2 },
  capsule: { flex: 1, minHeight: ROW_H, borderRadius: ROW_H / 2, justifyContent: 'center' },
  input: {
    maxHeight: 140, minHeight: ROW_H, color: color.tHi, fontSize: font.body,
    paddingHorizontal: space.s4, paddingTop: 12, paddingBottom: 12,
  },
  round: { width: ROW_H, height: ROW_H, borderRadius: ROW_H / 2, alignItems: 'center', justifyContent: 'center' },
  fallback: { fontSize: font.body, fontWeight: '700' },
  recInner: { height: ROW_H, flexDirection: 'row', alignItems: 'center', gap: space.s3, paddingHorizontal: space.s4 },
  recDot: { width: 10, height: 10, borderRadius: 5, backgroundColor: color.err },
  recTime: { color: color.tHi, fontSize: font.body, fontFamily: mono, fontVariant: ['tabular-nums'] },
  meter: { flex: 1, height: 6, borderRadius: 3, backgroundColor: 'rgba(255,255,255,0.12)', overflow: 'hidden' },
  meterFill: { height: 6, borderRadius: 3, backgroundColor: color.stListening },
  notice: { flexDirection: 'row', alignItems: 'center', gap: space.s3, paddingHorizontal: space.s2 },
  noticeText: { flex: 1, color: color.warn, fontSize: font.label },
  noticeAction: { color: color.stIdle, fontSize: font.label, fontWeight: '600' },
  off: { opacity: 0.4 },
  pressed: { opacity: 0.75 },
});
