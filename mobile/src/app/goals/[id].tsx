import { useLocalSearchParams } from 'expo-router';
import * as Haptics from 'expo-haptics';
import { useRef, useState } from 'react';
import { ActivityIndicator, Text, TextInput, View } from 'react-native';
import { answerGoal, controlGoal, getGoal, getGoalEvidence, getGoalFile, raiseGoalBudget } from '../../chat/client';
import { Markdown, openLink } from '../../chat/Markdown';
import type { GoalDetail, GoalEvidence, GoalFile } from '../../chat/types';
import { useCachedView, viewKey } from '../../core/useCachedView';
import { BudgetRing } from '../../goals/BudgetRing';
import { MilestonePath } from '../../goals/MilestonePath';
import { Question } from '../../goals/Question';
import { Button, explainGoal, Screen, Sheet } from '../../goals/Screen';
import { deadline, dollars, linkedEvidence, live, pollMs, validBudget } from '../../goals/model';
import { styles as s } from '../../goals/styles';
import { useApp } from '../../state/AppContext';
import { color } from '../../theme/tokens';
import { Glass } from '../../ui/Glass';

type Panel = { scope: string | null; title: string; file?: GoalFile; evidence?: GoalEvidence; error?: string };
export default function Goal() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const { cfg } = useApp();
  const [interval, setIntervalMs] = useState(60_000);
  const scope = viewKey(cfg, `goal:${id}`);
  const { data, error, stale, reload, setData, loaded } = useCachedView<GoalDetail>(id ? scope : null, async (cache) => {
    const view = await getGoal(cfg!, id, cache);
    setIntervalMs(pollMs(view.state));
    return view;
  }, { refreshMs: interval });
  const [busy, setBusy] = useState(false);
  const [recording, setRecording] = useState<string | null>(null);
  const [said, setSaid] = useState('');
  const [actionError, setError] = useState('');
  const [budget, setBudget] = useState('');
  const [cancel, setCancel] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [panel, setPanel] = useState<Panel | null>(null);
  const panelRequest = useRef(0);
  const closePanel = () => { panelRequest.current++; setPanel(null); };
  const act = async (run: () => Promise<{ text: string; goal: GoalDetail }>) => {
    if (busy || recording || !cfg) return false;
    setBusy(true); setError('');
    try {
      const result = await run(); setData(result.goal); setIntervalMs(pollMs(result.goal.state)); setSaid(result.text);
      void Haptics.notificationAsync(Haptics.NotificationFeedbackType.Success); return true;
    } catch (e) { setError(explainGoal(e)); return false; }
    finally { setBusy(false); }
  };
  const showFile = async (name: string) => {
    if (!cfg) return;
    const request = ++panelRequest.current;
    setPanel({ scope, title: name });
    try {
      const file = await getGoalFile(cfg, id, name);
      if (request === panelRequest.current) setPanel({ scope, title: name, file });
    } catch (e) { if (request === panelRequest.current) setPanel({ scope, title: name, error: explainGoal(e) }); }
  };
  const showEvidence = async (ref: string) => {
    if (!cfg || !data?.evidence.some((e) => e.id === ref)) return;
    const request = ++panelRequest.current;
    setPanel({ scope, title: ref });
    try {
      const evidence = await getGoalEvidence(cfg, id, ref);
      if (request === panelRequest.current) setPanel({ scope, title: ref, evidence });
    } catch (e) { if (request === panelRequest.current) setPanel({ scope, title: ref, error: explainGoal(e) }); }
  };
  const refresh = async () => { setRefreshing(true); await reload(true); setRefreshing(false); };
  const amount = data ? validBudget(budget, data.budget_usd) : null;
  const shownPanel = panel?.scope === scope ? panel : null;
  return <Screen title="Goal" error={actionError || (error ? explainGoal(error) : '')} stale={stale}
    loading={!!cfg && !loaded && !data} refreshing={refreshing} reload={() => void refresh()}>
    {!cfg ? <Text style={s.dim}>Pair with JARVIS in Settings first.</Text> : null}
    {data ? <>
      <View style={s.spread}><View style={s.grow}><Text style={s.status}>{data.state}</Text><Text style={s.heading}>{data.title}</Text>
        <Text style={s.dim}>{deadline(data.deadline_at)}</Text></View><BudgetRing spent={data.spent_usd} budget={data.budget_usd} /></View>
      {!data.enabled ? <Text style={s.dim}>{data.reason}</Text> : null}
      {data.notes.map((n, i) => <Text key={`${i}:${n}`} style={s.dim}>{n}</Text>)}
      {said ? <Text style={s.said}>{said}</Text> : null}
      <Text style={s.section}>Milestones</Text>
      {data.milestones.length ? <MilestonePath goal={data} onFile={(name) => void showFile(name)} /> : <Text style={s.dim}>The plan has not been written yet.</Text>}
      <Glass style={s.card}><Text style={s.section}>Now</Text>
        {data.session ? <><Text style={s.status}>{data.session.state}</Text>
          {data.session.steps.map((line, i) => <Text key={i} style={s.dim}>{line}</Text>)}
          {!data.session.steps.length ? <Text style={s.dim}>No steps recorded yet.</Text> : null}</> : <Text style={s.dim}>No session yet.</Text>}
        {data.waiting.map((w) => <Text key={w.wait_id} style={s.dim}>Waiting: {w.label}</Text>)}
      </Glass>
      {data.enabled && cfg ? data.questions.map((q) => <Question key={q.wait_id} question={q} cfg={cfg} disabled={busy}
        recording={recording} onRecording={setRecording} onAnswer={(wait, text) => act(() => answerGoal(cfg, wait, text))} />) : null}
      <Glass style={s.card}><Text style={s.section}>Budget</Text>
        <Text style={s.body}>{dollars(data.charges.spent_usd)} spent · {dollars(data.charges.reserved_usd)} reserved</Text>
        <Text style={s.dim}>{dollars(data.budget_usd)} total budget</Text>
        {data.enabled && live(data.state) && cfg ? <>
          <TextInput accessibilityLabel="New total budget in dollars" placeholder="New total budget ($)" value={budget} onChangeText={setBudget}
            keyboardType="decimal-pad" style={s.input} placeholderTextColor={color.tDim} editable={!busy && !recording} />
          <Button label="Raise budget" disabled={busy || !!recording || amount === null} onPress={() => amount !== null &&
            void act(() => raiseGoalBudget(cfg, id, amount)).then((ok) => { if (ok) setBudget(''); })} />
          <Text style={s.dim}>Set a higher total, up to $20. Raising a paused goal's budget lets it carry on.</Text>
        </> : null}
      </Glass>
      {data.enabled && live(data.state) && cfg ? <View style={s.buttons}>
        <Button label={data.state === 'paused' ? 'Resume' : 'Pause'} disabled={busy || !!recording}
          onPress={() => void act(() => controlGoal(cfg, id, data.state === 'paused' ? 'resume' : 'pause'))} />
        <Button label="Cancel" disabled={busy || !!recording} onPress={() => setCancel(true)} />
      </View> : null}
      {data.result ? <><Text style={s.section}>Result</Text>
        <Markdown text={linkedEvidence(data.result, data.evidence.map((e) => e.id))} onEvidence={(ref) => void showEvidence(ref)} />
        {data.result_truncated ? <Text style={s.dim}>Showing the first 100 KB.</Text> : null}</> : null}
      {data.outcome?.reason ? <Text style={s.dim}>{data.outcome.reason}</Text> : null}
      {data.files.length ? <Text style={s.section}>Files</Text> : null}
      {data.files.map((f) => <Button key={f.name} label={`Open ${f.name}`} onPress={() => void showFile(f.name)} />)}
    </> : null}
    {cancel && cfg ? <Sheet title="Cancel this goal?" onClose={() => setCancel(false)}>
      <Text style={s.body}>Work and spending stop. Saved notes and files are kept.</Text>
      <Button label="Keep goal" onPress={() => setCancel(false)} />
      <Button label="Cancel goal" onPress={() => { setCancel(false); void act(() => controlGoal(cfg, id, 'cancel')); }} />
    </Sheet> : null}
    {shownPanel ? <Sheet title={shownPanel.title} onClose={closePanel}>
      {shownPanel.error ? <Text style={s.error}>{shownPanel.error}</Text> : !shownPanel.file && !shownPanel.evidence ? <ActivityIndicator color={color.stIdle} /> : null}
      {shownPanel.file ? <><Text style={s.dim}>Revision {shownPanel.file.revision} · {shownPanel.file.size} bytes</Text>
        <Markdown text={shownPanel.file.name.endsWith('.md') ? shownPanel.file.text : `\`\`\`\n${shownPanel.file.text}\n\`\`\``} />
        {shownPanel.file.truncated ? <Text style={s.dim}>Showing the first 100 KB.</Text> : null}</> : null}
      {shownPanel.evidence ? <><Text style={s.heading}>{shownPanel.evidence.title}</Text>
        <Text style={s.dim}>Source: {shownPanel.evidence.tool}</Text>
        {shownPanel.evidence.url ? <Text style={s.link} accessibilityRole="link" onPress={() => openLink(shownPanel.evidence!.url)}>{shownPanel.evidence.url}</Text> : null}
        <Text style={s.body} selectable>{shownPanel.evidence.excerpt}</Text>
        {shownPanel.evidence.truncated ? <Text style={s.dim}>This source text was shortened.</Text> : null}</> : null}
    </Sheet> : null}
  </Screen>;
}
