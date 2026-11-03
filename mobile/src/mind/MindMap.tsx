import { Text, View } from 'react-native';
import type { MindView } from '../chat/types';
import { styles as s } from '../goals/styles';
import { SkiaBoundary, skiaAvailable } from '../ui/skia';
import { MindList } from './MindList';
import { visibleGraph } from './model';
import { useLayout } from './useLayout';

export type MapProps = { view: MindView; hidden: string[]; until: number; query: string; reduced: boolean; onPick: (id: string) => void; list?: boolean };
type Drawing = typeof import('./SkiaMindMap').SkiaMindMap;
let drawing: Drawing | null | undefined;
function nativeMap() {
  if (drawing !== undefined) return drawing;
  try { drawing = skiaAvailable() ? (require('./SkiaMindMap') as typeof import('./SkiaMindMap')).SkiaMindMap : null; }
  catch (error) { console.warn('Mind drawing unavailable; showing the note list.', error); drawing = null; }
  return drawing;
}
function LaidOutMap({ Drawing, ...props }: MapProps & { Drawing: Drawing }) {
  const layout = useLayout(props.view);
  return <View style={{ flex: 1 }}>
    {!layout?.ready ? <Text style={[s.dim, { paddingHorizontal: 16 }]}>Arranging his notes… You can still browse the list.</Text> : null}
    {layout?.ready ? <Drawing {...props} points={layout.points} /> : null}
  </View>;
}
export function MindMap(props: MapProps) {
  const graph = visibleGraph(props.view, props.hidden, props.until);
  const fallback = <MindList nodes={graph.nodes} view={{ ...props.view, ...graph }} query={props.query} onPick={props.onPick} />;
  const Drawing = props.list ? null : nativeMap();
  return Drawing ? <SkiaBoundary fallback={fallback}><LaidOutMap {...props} Drawing={Drawing} /></SkiaBoundary> : fallback;
}
