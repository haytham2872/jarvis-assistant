import { router, useFocusEffect } from 'expo-router';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { ActivityIndicator, AppState, Pressable, ScrollView, Text, TextInput, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import { getMind } from '../../chat/client';
import { useReduceMotion } from '../../chat/Orb';
import type { MindView } from '../../chat/types';
import { describeFailure } from '../../core/api';
import { useCachedView, viewKey } from '../../core/useCachedView';
import { Button, Sheet } from '../../goals/Screen';
import { styles as s } from '../../goals/styles';
import { MindMap } from '../../mind/MindMap';
import { count, folderLabel, folders, linked, mapDate, matches, visibleGraph } from '../../mind/model';
import { TimeSlider } from '../../mind/TimeSlider';
import { useApp } from '../../state/AppContext';
import { color, space } from '../../theme/tokens';
import { ReadFrame } from '../../ui/ReadFrame';

function MapBody({ view, onRefresh }: { view: MindView; onRefresh: () => void }) {
  const reduced = useReduceMotion(), insets = useSafeAreaInsets();
  const end = useMemo(() => Math.max(Date.now() / 1000, ...view.nodes.map((n) => n.created)), [view.nodes]);
  const start = useMemo(() => Math.min(end, ...view.nodes.map((n) => n.created)), [view.nodes, end]);
  const [until, setUntil] = useState<number | null>(null), [hidden, setHidden] = useState<string[]>([]);
  const [query, setQuery] = useState(''), [selected, setSelected] = useState<string | null>(null), [list, setList] = useState(false);
  const [playing, setPlaying] = useState(false), [focused, setFocused] = useState(false);
  useFocusEffect(useCallback(() => { setFocused(true); return () => { setFocused(false); setPlaying(false); }; }, []));
  useEffect(() => {
    const listener = AppState.addEventListener('change', (state) => { if (state !== 'active') setPlaying(false); });
    return () => listener.remove();
  }, []);
  const at = until === null ? end : Math.max(start, Math.min(end, until));
  useEffect(() => {
    if (!playing || !focused) return;
    const timer = setInterval(() => setUntil((current) => {
      const next = Math.min(end, (current ?? start) + Math.max(86400, (end - start) / 60));
      if (next === end) setPlaying(false);
      return next;
    }), 180);
    return () => clearInterval(timer);
  }, [playing, focused, start, end]);
  const graph = useMemo(() => visibleGraph(view, hidden, at), [view, hidden, at]);
  const node = graph.nodes.find((n) => n.id === selected);
  const availableFolders = Object.keys(folders).filter((f) => view.nodes.some((n) => n.folder === f));
  return <>
    <View style={{ paddingHorizontal: space.s4, gap: space.s2, paddingBottom: space.s2 }}>
      <View style={s.spread}><Text style={s.dim}>{count(graph.nodes.length, 'note')}{view.truncated ? ` of ${view.total}` : ''} · {count(graph.edges.length, 'connection')}</Text>
        <View style={{ flexDirection: 'row', gap: space.s4 }}>
          <Pressable accessibilityRole="button" accessibilityLabel="Refresh mind map" onPress={onRefresh} hitSlop={8} style={{ minHeight: 36, justifyContent: 'center' }}>
            <Text style={s.link}>Refresh</Text></Pressable>
          <Pressable accessibilityRole="button" accessibilityLabel={list ? 'Show map' : 'Show list'} onPress={() => setList(!list)} hitSlop={8} style={{ minHeight: 36, justifyContent: 'center' }}>
            <Text style={s.link}>{list ? 'Map' : 'List'}</Text></Pressable></View></View>
      <TextInput style={[s.input, { minHeight: 40, paddingVertical: space.s2 }]} accessibilityLabel="Find a note" placeholder="Find a thought, a person, a question…" placeholderTextColor={color.tMid}
        value={query} onChangeText={setQuery} autoCorrect={false} />
      {query.trim() ? <Text style={s.dim}>{graph.nodes.filter((n) => matches(n, query)).length} matching titles · highlighted in the map and list</Text> : null}
      <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={{ gap: space.s2 }}>
        {availableFolders.map((f) => <Pressable key={f} accessibilityRole="checkbox" accessibilityState={{ checked: !hidden.includes(f) }}
          accessibilityLabel={folderLabel(f)} onPress={() => setHidden((old) => old.includes(f) ? old.filter((x) => x !== f) : [...old, f])}
          hitSlop={{ top: 6, bottom: 6 }} style={{ minHeight: 32, justifyContent: 'center', paddingHorizontal: space.s3, borderRadius: 16, borderWidth: 1, borderColor: hidden.includes(f) ? color.border : folders[f].color, opacity: hidden.includes(f) ? .5 : 1 }}>
          <Text style={s.dim}>{folderLabel(f)}</Text></Pressable>)}
      </ScrollView>
      {view.notes.length ? <ScrollView style={{ maxHeight: 76 }}>{view.notes.map((n) => <Text key={n} style={s.error} accessibilityRole="alert">{n}</Text>)}</ScrollView> : null}
    </View>
    {!view.nodes.length ? <View style={{ padding: space.s5 }}><Text style={s.heading}>{view.complete ? 'A mind taking shape.' : 'The map is incomplete.'}</Text>
      <Text style={s.dim}>{view.complete ? 'Notes and connections will appear here as JARVIS writes them.' : 'Some notes could not be read. Try refreshing.'}</Text></View> : null}
    {!graph.nodes.length && !!view.nodes.length ? <Text style={[s.dim, { padding: space.s4 }]}>No notes at this date with these filters.</Text> : null}
    <MindMap view={view} hidden={hidden} until={at} query={query} reduced={reduced} onPick={setSelected} list={list} />
    <View style={{ paddingHorizontal: space.s5, paddingBottom: insets.bottom + space.s2, gap: space.s1 }}>
      <View style={s.spread}><Text style={s.dim}>{until === null || at >= end ? 'Today' : mapDate(at)}</Text>
        <Button label={playing ? 'Pause' : 'Play growth'} disabled={!view.nodes.length || start === end} onPress={() => {
          if (playing) setPlaying(false); else { setUntil(start); setPlaying(true); }
        }} /></View>
      <TimeSlider start={start} end={end} value={at} onChange={(value) => { setPlaying(false); setUntil(value); }} />
      <Text style={[s.dim, { fontSize: 12, lineHeight: 16 }]}>{view.truncated ? 'Older letters with no connection are left out. ' : ''}Play replays when notes appeared (modification date where creation is unknown), not their past contents.</Text>
    </View>
    {node ? <Sheet title={node.title} onClose={() => setSelected(null)}>
      <Text style={s.status}>{folderLabel(node.folder)}{node.state ? ` · ${node.state}` : ''}</Text>
      <Text style={s.dim}>{node.created_source === 'modified' ? 'Creation not recorded; placed at' : 'Created'} {mapDate(node.created)}</Text>
      <Text style={s.dim}>Updated {mapDate(node.updated)} · Paris</Text>
      <Button label="Open note" onPress={() => { setSelected(null); router.push({ pathname: '/mind/note', params: node.link }); }} />
      <Text style={s.heading}>Connected notes</Text>
      {!linked({ ...view, ...graph }, node.id).length ? <Text style={s.dim}>No resolved connections yet.</Text> : null}
      {linked({ ...view, ...graph }, node.id).map((n) => <Pressable key={n.id} accessibilityRole="button" accessibilityLabel={`Jump to ${n.title}`} onPress={() => setSelected(n.id)} style={{ minHeight: 44, justifyContent: 'center' }}>
        <Text style={s.link}>{n.title}</Text><Text style={s.dim}>{folderLabel(n.folder)}</Text></Pressable>)}
    </Sheet> : null}
  </>;
}
export default function MindScreen() {
  const { cfg } = useApp(), key = viewKey(cfg, 'mind');
  const { data, error, stale, loaded, reload } = useCachedView<MindView>(key, (cache) => getMind(cfg!, cache), { refreshMs: 60_000 });
  const failure = error as { status?: number; message?: string };
  return <ReadFrame title="His mind">
    <View style={{ paddingHorizontal: space.s4 }}>
      {!cfg ? <Text style={s.dim}>Pair with JARVIS in Settings first.</Text> : null}
      {error ? <Text accessibilityRole="alert" style={s.error}>{failure.status === 404 ? 'This server does not have the mind map yet. Update it and restart JARVIS.' : describeFailure(failure.status ?? null, failure.message ?? '')}</Text> : null}
      {stale ? <Text style={s.dim}>{stale}</Text> : null}
      {cfg && !data && !loaded ? <ActivityIndicator color={color.stIdle} accessibilityLabel="Loading mind map" /> : null}
      {cfg && !data ? <Pressable accessibilityRole="button" accessibilityLabel="Refresh mind map" onPress={() => void reload(true)} style={{ alignSelf: 'flex-end', minHeight: 44, justifyContent: 'center' }}><Text style={s.link}>Refresh</Text></Pressable> : null}
    </View>
    {data ? <MapBody key={key} view={data} onRefresh={() => void reload(true)} /> : null}
  </ReadFrame>;
}
