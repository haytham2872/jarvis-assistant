// What JARVIS did on a turn, in one fold under it (slice 1 merges the plan panel and the steps
// list, which sat under each answer as two separate folds): the plan he followed, each step
// pending, running, done or failed; the tools he called; and, while he works, his thinking.
// Open while the turn runs, one line once it is answered, unless the owner opens it.
import { useState } from 'react';
import { Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';

import { color, font, mono, radius, space } from '../theme/tokens';
import type { Step } from './reducer';
import type { PlanStep } from './types';

const GLYPH: Record<PlanStep['status'], string> = { pending: '○', running: '◉', done: '✓', failed: '✕' };
type ToolStep = Extract<Step, { kind: 'tool' }>;

export function planSummary(plan: PlanStep[]): string {
  const done = plan.filter((s) => s.status === 'done').length;
  const failed = plan.filter((s) => s.status === 'failed').length;
  return `Plan · ${done} of ${plan.length} done${failed ? ` · ${failed} failed` : ''}`;
}

/** The one line the fold shows: the plan's progress, then how many steps and which tools. */
export function workSummary(plan: PlanStep[] | undefined, steps: Step[] | undefined): string {
  const tools = (steps ?? []).filter((s): s is ToolStep => s.kind === 'tool');
  const count = `${tools.length} step${tools.length === 1 ? '' : 's'}`;
  if (plan?.length) return tools.length ? `${planSummary(plan)} · ${count}` : planSummary(plan);
  if (tools.length) return `${count} · ${[...new Set(tools.map((s) => s.tool))].join(', ')}`;
  return steps?.length ? 'Notes' : '';
}

type Props = { plan?: PlanStep[]; steps?: Step[]; thinking?: string; running: boolean };

export function Work({ plan, steps, thinking, running }: Props) {
  const [open, setOpen] = useState<boolean | null>(null);
  const [thoughtsOpen, setThoughtsOpen] = useState(false);
  const summary = workSummary(plan, steps);
  const hasThinking = running && !!thinking?.trim();
  if (!summary && !hasThinking) return null;
  const expanded = open ?? running;            // open while working, folded after, unless he chose
  return (
    <View style={[styles.box, running && styles.boxRunning]}>
      {summary ? (
        <Pressable accessibilityRole="button" accessibilityState={{ expanded }} hitSlop={8}
          onPress={() => setOpen(!expanded)} accessibilityLabel={`${summary}. ${expanded ? 'Hide' : 'Show'} details`}>
          <Text style={styles.head}>{expanded ? '▾' : '▸'} {summary}</Text>
        </Pressable>
      ) : null}
      {expanded && plan?.length ? plan.map((s, i) => (
        <View key={`p${s.id}-${i}`} style={styles.row}>
          <Text style={[styles.glyph, s.status === 'running' && styles.running, s.status === 'done' && styles.done,
            s.status === 'failed' && styles.failed]}>{GLYPH[s.status]}</Text>
          <Text style={[styles.label, s.status === 'pending' && styles.pending]}>
            {s.label}{s.tool ? <Text style={styles.tool}>{`  ${s.tool}`}</Text> : null}
          </Text>
        </View>
      )) : null}
      {expanded && steps?.length ? (
        <View style={plan?.length ? styles.stepsUnderPlan : undefined}>
          {steps.map((s, i) => s.kind === 'say'
            ? <Text key={`s${i}`} style={styles.say} selectable>{s.text}</Text>
            : <Text key={`s${i}`} style={[styles.step, s.outcome === 'failed' && { color: color.err }]} selectable>
                {s.outcome === 'failed' ? '✕ ' : s.outcome === 'unknown' ? '? ' : '• '}{s.tool}{s.detail ? ` — ${s.detail}` : ''}
              </Text>)}
        </View>
      ) : null}
      {hasThinking ? (
        <Pressable onPress={() => setThoughtsOpen((o) => !o)} accessibilityRole="button" hitSlop={6}
          accessibilityLabel={thoughtsOpen ? 'Hide thinking' : 'Show all thinking'}>
          {thoughtsOpen ? (
            <ScrollView style={styles.thoughtsBox} nestedScrollEnabled>
              <Text style={styles.thoughts} selectable>{thinking}</Text>
            </ScrollView>
          ) : <Text style={styles.thoughts} numberOfLines={2}>{thinking!.trim().split('\n').slice(-3).join(' ')}</Text>}
        </Pressable>
      ) : null}
    </View>
  );
}

const styles = StyleSheet.create({
  box: {
    gap: space.s1, paddingLeft: space.s3, paddingVertical: 2,
    borderLeftWidth: 2, borderLeftColor: color.border, borderRadius: radius.sm,
  },
  boxRunning: { borderLeftColor: color.stThinking },
  head: { color: color.tMid, fontSize: font.label },
  row: { flexDirection: 'row', gap: space.s2 },
  glyph: { color: color.tDim, fontSize: font.label, width: 14, textAlign: 'center' },
  running: { color: color.stThinking },
  done: { color: color.ok },
  failed: { color: color.err },
  label: { flex: 1, color: color.tHi, fontSize: font.label, lineHeight: 18 },
  pending: { color: color.tMid },
  tool: { color: color.tDim, fontFamily: mono, fontSize: font.micro },
  stepsUnderPlan: { marginTop: space.s1, paddingTop: space.s1, borderTopColor: color.border, borderTopWidth: StyleSheet.hairlineWidth, gap: 2 },
  step: { color: color.tMid, fontSize: font.label, lineHeight: 18 },
  say: { color: color.tDim, fontSize: font.label, fontStyle: 'italic', lineHeight: 18 },
  thoughts: { color: color.tDim, fontSize: font.label, fontStyle: 'italic', lineHeight: 18 },
  thoughtsBox: { maxHeight: 220 },
});
