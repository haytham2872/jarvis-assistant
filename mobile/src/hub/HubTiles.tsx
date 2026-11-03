import { router } from 'expo-router';
import { SymbolView } from 'expo-symbols';
import { Pressable, Text, View } from 'react-native';
import { getHub } from '../chat/client';
import type { HubView } from '../chat/types';
import { useCachedView, viewKey } from '../core/useCachedView';
import { hubLine } from '../goals/model';
import { hubRecord } from '../record/model';
import { hubOvernight } from '../overnight/model';
import { hubMind } from '../mind/model';
import { useLastLooked } from '../overnight/lastLooked';
import { styles as s } from '../goals/styles';
import { useApp } from '../state/AppContext';
import { color, space } from '../theme/tokens';
import { Glass } from '../ui/Glass';

/** Later slices append their tiles only when their routes are ready. */
export function HubTiles() {
  const { cfg } = useApp();
  const looked = useLastLooked(viewKey(cfg, 'overnight-looked'));
  const { data, error, reload } = useCachedView<HubView>(looked.loaded ? viewKey(cfg, `hub:${looked.at ?? 'default'}`) : null,
    (cache) => getHub(cfg!, cache, looked.at), { refreshMs: 30_000 });
  if (!cfg) return null;
  return <View style={{ gap: space.s2 }}>
    <Pressable accessibilityRole="button" accessibilityLabel={`Goals, ${hubLine(data?.goals)}`}
      onPress={() => router.push('/goals')} style={({ pressed }) => pressed && s.pressed}>
      <Glass tone="blue" interactive style={[s.card, s.row]}>
        <SymbolView name="flag.checkered" size={26} tintColor={color.stIdle} fallback={<Text style={s.link}>G</Text>} />
        <View style={s.grow}><Text style={s.heading}>Goals</Text><Text style={s.dim}>{hubLine(data?.goals)}</Text></View>
        <SymbolView name="chevron.right" size={14} tintColor={color.tMid} fallback={<Text style={s.dim}>›</Text>} />
      </Glass>
    </Pressable>
    {data?.overnight ? <Pressable accessibilityRole="button" accessibilityLabel={`While you slept, ${hubOvernight(data.overnight)}`}
      onPress={() => router.push('/overnight')} style={({ pressed }) => pressed && s.pressed}>
      <Glass tone="blue" interactive style={[s.card, s.row]}><SymbolView name="moon.stars" size={26} tintColor={color.stIdle} fallback={<Text style={s.link}>N</Text>} />
        <View style={s.grow}><Text style={s.heading}>While you slept</Text><Text style={s.dim}>{hubOvernight(data.overnight)}</Text></View>
        <SymbolView name="chevron.right" size={14} tintColor={color.tMid} fallback={<Text style={s.dim}>›</Text>} /></Glass>
    </Pressable> : null}
    {data?.record ? <Pressable accessibilityRole="button" accessibilityLabel={`Track record, ${hubRecord(data.record)}`}
      onPress={() => router.push('/record')} style={({ pressed }) => pressed && s.pressed}>
      <Glass tone="blue" interactive style={[s.card, s.row]}><SymbolView name="chart.xyaxis.line" size={26} tintColor={color.stIdle} fallback={<Text style={s.link}>R</Text>} />
        <View style={s.grow}><Text style={s.heading}>Track record</Text><Text style={s.dim}>{hubRecord(data.record)}</Text></View>
        <SymbolView name="chevron.right" size={14} tintColor={color.tMid} fallback={<Text style={s.dim}>›</Text>} /></Glass>
    </Pressable> : null}
    {data?.mind ? <Pressable accessibilityRole="button" accessibilityLabel={`Mind, ${hubMind(data.mind)}`}
      onPress={() => router.push('/mind')} style={({ pressed }) => pressed && s.pressed}>
      <Glass tone="blue" interactive style={[s.card, s.row]}><SymbolView name="point.3.connected.trianglepath.dotted" size={26} tintColor={color.stIdle} fallback={<Text style={s.link}>M</Text>} />
        <View style={s.grow}><Text style={s.heading}>Mind</Text><Text style={s.dim}>{hubMind(data.mind)}</Text></View>
        <SymbolView name="chevron.right" size={14} tintColor={color.tMid} fallback={<Text style={s.dim}>›</Text>} /></Glass>
    </Pressable> : null}
    {data?.notes?.map((n) => <Text key={n} style={s.dim}>{n}</Text>)}
    {error ? <Pressable accessibilityRole="button" accessibilityLabel="Refresh activity counts" onPress={() => void reload()}>
      <Text style={s.dim}>Activity counts could not refresh. Tap to try again.</Text></Pressable> : null}
  </View>;
}
