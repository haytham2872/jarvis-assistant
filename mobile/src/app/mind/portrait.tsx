import { ActivityIndicator, ScrollView, Text, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import { getPortrait } from '../../chat/client';
import type { PortraitView } from '../../chat/types';
import { describeFailure } from '../../core/api';
import { useCachedView, viewKey } from '../../core/useCachedView';
import { styles as s } from '../../goals/styles';
import { useApp } from '../../state/AppContext';
import { color, space } from '../../theme/tokens';
import { ReadFrame } from '../../ui/ReadFrame';

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const parts = (iso: string | null) => { const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso ?? ''); return m ? { y: +m[1], m: +m[2], d: +m[3] } : null; };
const short = (iso: string | null) => { const p = parts(iso); return p ? `${p.d} ${MONTHS[p.m - 1]}` : ''; };
const ageDays = (iso: string | null) => { const p = parts(iso); return p ? (Date.now() - Date.UTC(p.y, p.m - 1, p.d)) / 86400000 : 0; };

function Body({ view }: { view: PortraitView }) {
  if (!view.migrated) return <Text style={s.dim}>Your portrait moves to the new format after your next conversation with JARVIS.</Text>;
  return <>
    {view.sections.map((sec) => <View key={sec.title} style={{ gap: space.s2 }}>
      <Text style={s.heading} accessibilityRole="header">{sec.title}</Text>
      {!sec.entries.length ? <Text style={s.dim}>Nothing here yet.</Text> : null}
      {sec.entries.map((e, i) => <View key={`${i}-${e.text}`} style={{ gap: 2 }}>
        <Text style={{ color: color.tHi }}>{e.text}</Text>
        <Text style={s.dim}>{e.as_of ? `as of ${short(e.as_of)}` : ''}{sec.title === 'Situation' && e.as_of && ageDays(e.as_of) > view.fading_days - 7 ? ' · fading soon' : ''}</Text>
      </View>)}
    </View>)}
    {view.faded.length ? <View style={{ gap: space.s2 }}>
      <Text style={s.heading} accessibilityRole="header">Faded</Text>
      {view.faded.map((g) => <View key={g.faded_on} style={{ gap: space.s2 }}>
        {g.entries.map((e, i) => <Text key={`${i}-${e.text}`} style={s.dim}>{e.text} ({e.as_of ? `${short(e.as_of)} – ` : ''}faded {short(g.faded_on)})</Text>)}
      </View>)}
    </View> : null}
  </>;
}

export default function PortraitScreen() {
  const { cfg } = useApp(), key = viewKey(cfg, 'portrait'), insets = useSafeAreaInsets();
  const { data, error, stale, loaded } = useCachedView<PortraitView>(key, (cache) => getPortrait(cfg!, cache), { refreshMs: 60_000 });
  const failure = error as { status?: number; message?: string };
  return <ReadFrame title="How JARVIS sees you">
    <ScrollView contentContainerStyle={{ paddingHorizontal: space.s4, gap: space.s4, paddingBottom: insets.bottom + space.s5 }}>
      {!cfg ? <Text style={s.dim}>Pair with JARVIS in Settings first.</Text> : null}
      {error ? <Text accessibilityRole="alert" style={s.error}>{failure.status === 404 ? 'This server does not have the portrait view yet. Update it and restart JARVIS.' : describeFailure(failure.status ?? null, failure.message ?? '')}</Text> : null}
      {stale ? <Text style={s.dim}>{stale}</Text> : null}
      {cfg && !data && !loaded ? <ActivityIndicator color={color.stIdle} accessibilityLabel="Loading portrait" /> : null}
      {data ? <Body view={data} /> : null}
    </ScrollView>
  </ReadFrame>;
}
