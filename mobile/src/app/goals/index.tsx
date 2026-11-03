import { router } from 'expo-router';
import * as Haptics from 'expo-haptics';
import { useState } from 'react';
import { Pressable, Text, View } from 'react-native';
import { decideGoalProposal, getGoals } from '../../chat/client';
import type { GoalsView } from '../../chat/types';
import { useCachedView, viewKey } from '../../core/useCachedView';
import { BudgetRing } from '../../goals/BudgetRing';
import { Button, explainGoal, Screen } from '../../goals/Screen';
import { deadline, dollars, nowLine, sortGoals } from '../../goals/model';
import { styles as s } from '../../goals/styles';
import { useApp } from '../../state/AppContext';
import { Glass } from '../../ui/Glass';

export default function Goals() {
  const { cfg } = useApp();
  const { data, error, stale, reload, loaded } = useCachedView<GoalsView>(viewKey(cfg, 'goals'), (cache) => getGoals(cfg!, cache), { refreshMs: 60_000 });
  const [busy, setBusy] = useState<string | null>(null);
  const [said, setSaid] = useState('');
  const [actionError, setError] = useState('');
  const [refreshing, setRefreshing] = useState(false);
  const decide = async (id: string, start: boolean) => {
    if (!cfg || busy) return;
    setBusy(id); setError('');
    try { setSaid((await decideGoalProposal(cfg, id, start)).text); void Haptics.notificationAsync(Haptics.NotificationFeedbackType.Success); await reload(true); }
    catch (e) { setError(explainGoal(e)); }
    finally { setBusy(null); }
  };
  const refresh = async () => { setRefreshing(true); await reload(true); setRefreshing(false); };
  return <Screen title="Goals" error={actionError || (error ? explainGoal(error) : '')} stale={stale}
    loading={!!cfg && !loaded && !data} refreshing={refreshing} reload={() => void refresh()}>
    {!cfg ? <Text style={s.dim}>Pair with JARVIS in Settings first.</Text> : null}
    {data?.enabled === false ? <Text style={s.dim}>{data.reason}</Text> : null}
    {said ? <Text style={s.said}>{said}</Text> : null}
    {data?.enabled ? <>
      {data.notes.map((n) => <Text key={n} style={s.dim}>{n}</Text>)}
      {data.proposals.length ? <Text style={s.section}>Offered to you</Text> : null}
      {data.proposals.map((p) => <Glass key={p.goal_id} tone="blue" style={s.card}>
        <Text style={s.status}>Ready for your decision</Text><Text style={s.heading}>{p.title}</Text>
        {p.criteria.map((c, i) => <Text key={i} style={s.dim}>• {c}</Text>)}
        <Text style={s.dim}>{dollars(p.budget_usd)} budget{p.deadline ? ` · ${deadline(Date.parse(p.deadline) / 1000)}` : ''}</Text>
        <View style={s.buttons}><Button label="Start" accessibilityLabel={`Start ${p.title}`} disabled={!!busy} accent onPress={() => void decide(p.goal_id, true)} />
          <Button label="Not now" accessibilityLabel={`Not now: ${p.title}`} disabled={!!busy} onPress={() => void decide(p.goal_id, false)} /></View>
      </Glass>)}
      {data.goals.length ? <Text style={s.section}>Your goals</Text> : <Text style={s.dim}>No goals yet. Ask JARVIS about something you want to work on.</Text>}
      {sortGoals(data.goals).map((g) => <Pressable key={g.goal_id} accessibilityRole="button" accessibilityLabel={`Open goal ${g.title}`}
        onPress={() => router.push(`/goals/${encodeURIComponent(g.goal_id)}`)} style={({ pressed }) => pressed && s.pressed}>
        <Glass style={s.card}><View style={s.spread}><View style={s.grow}><Text style={s.status}>{g.state}</Text><Text style={s.heading}>{g.title}</Text></View>
          <BudgetRing spent={g.spent_usd} budget={g.budget_usd} /></View>
          <Text style={s.body}>{g.milestones.done} of {g.milestones.total} milestones</Text>
          <Text style={s.dim}>{dollars(g.spent_usd)} used or reserved · {dollars(g.budget_usd)} budget</Text>
          <Text style={s.dim}>{deadline(g.deadline_at)}</Text>
          <Text style={s.dim}>{nowLine(g)}</Text>
        </Glass>
      </Pressable>)}
    </> : null}
  </Screen>;
}
