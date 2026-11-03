// M4: what JARVIS needs from the owner, and what he is doing in the background.
//
// ApprovalTray sits above the composer while a request waits: the same request he would get
// on Discord, decided with the same functions (one conditional claim on the server, so a
// double tap, or a tap racing Discord, runs the action at most once). JobCard is one piece
// of background work started from the phone, in the conversation where it was asked for.
import * as Haptics from 'expo-haptics';
import { useEffect, useState } from 'react';
import { ActivityIndicator, Pressable, ScrollView, StyleSheet, Text, useWindowDimensions, View } from 'react-native';

import { color, font, radius, space } from '../theme/tokens';
import { Markdown } from './Markdown';
import type { Approval, Job } from './types';

type Decide = (id: number, approve: boolean) => Promise<string>;

const RESULT_MS = 5_000;

export function ApprovalTray({ approvals, onDecide }: { approvals: Approval[]; onDecide: Decide }) {
  const { height } = useWindowDimensions();
  if (!approvals.length) return null;
  // Several waiting requests scroll within 40% of the screen: the conversation stays visible.
  return (
    <ScrollView style={{ maxHeight: height * 0.4, flexGrow: 0 }} contentContainerStyle={styles.tray}
      accessibilityLabel="Waiting for your approval">
      {approvals.map((a) => <ApprovalCard key={a.id} approval={a} onDecide={onDecide} />)}
    </ScrollView>
  );
}

function ApprovalCard({ approval, onDecide }: { approval: Approval; onDecide: Decide }) {
  const [busy, setBusy] = useState<'approve' | 'deny' | null>(null);
  const [result, setResult] = useState('');
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!result) return;
    const t = setTimeout(() => setResult(''), RESULT_MS);
    return () => clearTimeout(t);
  }, [result]);

  const decide = async (approve: boolean) => {
    if (busy) return;
    setBusy(approve ? 'approve' : 'deny');
    void Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Medium);
    try {
      setResult(await onDecide(approval.id, approve));
    } catch (e) {
      setResult(e instanceof Error ? e.message : 'That did not go through. Try again.');
    } finally {
      setBusy(null);
    }
  };

  const running = approval.state === 'executing';
  return (
    <View style={styles.card}>
      <Text style={styles.cardHead}>{running ? `Doing it (#${approval.id})…` : `Needs your OK (#${approval.id})`}</Text>
      <Pressable onPress={() => setOpen((o) => !o)} accessibilityRole="button"
        accessibilityLabel={`${approval.preview}. ${open ? 'Show less' : 'Show all'}`}>
        <Text style={styles.preview} numberOfLines={open ? undefined : 4} selectable>{approval.preview}</Text>
      </Pressable>
      {approval.why ? <Text style={styles.why}>{approval.why}</Text> : null}
      {result ? <Text style={styles.result} accessibilityLiveRegion="polite">{result}</Text> : null}
      {!running ? (
        <View style={styles.buttons}>
          <Btn label="Deny" onPress={() => void decide(false)} busy={busy === 'deny'} disabled={!!busy} tone="plain" />
          <Btn label="Approve" onPress={() => void decide(true)} busy={busy === 'approve'} disabled={!!busy} tone="accent" />
        </View>
      ) : <ActivityIndicator size="small" color={color.tMid} style={styles.left} />}
    </View>
  );
}

function Btn(p: { label: string; onPress: () => void; busy: boolean; disabled: boolean; tone: 'accent' | 'plain' }) {
  const accent = p.tone === 'accent';
  return (
    <Pressable accessibilityRole="button" accessibilityLabel={p.label} onPress={p.onPress} disabled={p.disabled}
      style={({ pressed }) => [styles.btn, accent ? styles.btnAccent : styles.btnPlain,
        p.disabled && !p.busy && styles.off, pressed && styles.pressed]}>
      {p.busy ? <ActivityIndicator size="small" color={accent ? color.bg : color.tHi} />
        : <Text style={[styles.btnText, accent && styles.btnTextAccent]}>{p.label}</Text>}
    </Pressable>
  );
}

const JOB_PREVIEW_CHARS = 600;

/** What a finished job says as a normal message under its card: its conclusion only, or ''
 * (the owner, 15 October: the whole research stays folded in the card). */
export function jobConclusion(job: Job): string {
  return job.status === 'done' ? (job.conclusion || '').trim() : '';
}

export function JobCard({ job }: { job: Job }) {
  const [open, setOpen] = useState(false);
  const working = job.status === 'queued' || job.status === 'running';
  const failed = job.status === 'failed' || job.status === 'cancelled';
  // The whole result stays in the card, folded; only its conclusion is a message (jobConclusion).
  const body = job.status === 'done' ? (job.result || job.summary) : job.summary;
  const long = body.length > JOB_PREVIEW_CHARS;
  const shown = long && !open ? `${body.slice(0, JOB_PREVIEW_CHARS).trimEnd()}…` : body;
  const label = working ? (job.status === 'queued' ? 'Waiting to start' : 'Working in the background')
    : failed ? 'Background work stopped' : 'Background work done';
  return (
    <View style={[styles.job, failed && styles.jobFailed]}>
      <View style={styles.jobHead}>
        {working ? <ActivityIndicator size="small" color={color.tMid} />
          : <Text style={[styles.jobMark, failed && { color: color.err }]}>{failed ? '✕' : '✓'}</Text>}
        <Text style={styles.jobLabel}>{label}</Text>
      </View>
      <Text style={styles.jobTask} numberOfLines={3}>{job.description}</Text>
      {shown ? (failed ? <Text style={[styles.why, { color: color.err }]}>{shown}</Text> : <Markdown text={shown} />) : null}
      {long ? (
        <Pressable onPress={() => setOpen((o) => !o)} accessibilityRole="button" hitSlop={8}>
          <Text style={styles.more}>{open ? 'Show less' : 'Show all'}</Text>
        </Pressable>
      ) : null}
      {job.note_file && job.status === 'done' ? <Text style={styles.note}>Saved as {job.note_file}</Text> : null}
    </View>
  );
}

const styles = StyleSheet.create({
  tray: { gap: space.s2, paddingHorizontal: space.s3, paddingTop: space.s2 },
  card: {
    gap: space.s2, padding: space.s3, borderRadius: radius.lg, backgroundColor: color.surface,
    borderColor: color.warn, borderWidth: StyleSheet.hairlineWidth,
  },
  cardHead: { color: color.warn, fontSize: font.label, fontWeight: '600', letterSpacing: 0.5 },
  preview: { color: color.tHi, fontSize: font.body, lineHeight: 21 },
  why: { color: color.tMid, fontSize: font.label, lineHeight: 18 },
  result: { color: color.tHi, fontSize: font.label, lineHeight: 18 },
  buttons: { flexDirection: 'row', justifyContent: 'flex-end', gap: space.s2 },
  btn: { minHeight: 40, minWidth: 96, paddingHorizontal: space.s4, borderRadius: radius.lg, alignItems: 'center', justifyContent: 'center' },
  btnAccent: { backgroundColor: color.stIdle },
  btnPlain: { backgroundColor: color.surface2, borderColor: color.border, borderWidth: StyleSheet.hairlineWidth },
  btnText: { color: color.tHi, fontSize: font.body, fontWeight: '600' },
  btnTextAccent: { color: color.bg },
  left: { alignSelf: 'flex-start' },
  job: {
    gap: space.s2, padding: space.s3, borderRadius: radius.lg, backgroundColor: color.surface,
    borderColor: color.border, borderWidth: StyleSheet.hairlineWidth,
  },
  jobFailed: { borderColor: color.err },
  jobHead: { flexDirection: 'row', alignItems: 'center', gap: space.s2 },
  jobMark: { color: color.ok, fontSize: font.body, fontWeight: '700' },
  jobLabel: { color: color.tMid, fontSize: font.label, fontWeight: '600', letterSpacing: 0.5 },
  jobTask: { color: color.tHi, fontSize: font.body, fontWeight: '600' },
  more: { color: color.stIdle, fontSize: font.label },
  note: { color: color.tDim, fontSize: font.micro },
  off: { opacity: 0.4 },
  pressed: { opacity: 0.7 },
});
