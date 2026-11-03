import { useMemo } from 'react';
import { Pressable, Text, View } from 'react-native';
import type { MindNode, MindView } from '../chat/types';
import { styles as s } from '../goals/styles';
import { space } from '../theme/tokens';
import { ReadList } from '../ui/ReadList';
import { count, folderLabel, linked, matches } from './model';

export function MindList({ nodes, view, query, onPick }: { nodes: MindNode[]; view: MindView; query: string; onPick: (id: string) => void }) {
  const rows = useMemo(() => {
    const groups = [...new Set(nodes.map((n) => n.folder))].sort();
    return groups.flatMap((folder) => [folder, ...nodes.filter((n) => n.folder === folder).sort((a, b) => a.title.localeCompare(b.title))]);
  }, [nodes]);
  return <ReadList testID="mind-list" data={rows} keyExtractor={(row) => typeof row === 'string' ? row : row.id}
    initialNumToRender={12} windowSize={5} maxToRenderPerBatch={10} contentContainerStyle={{ padding: space.s4, gap: space.s3 }}
    renderItem={({ item }) => typeof item === 'string' ? <Text style={s.section}>{folderLabel(item)}</Text> :
      <View style={{ gap: space.s1 }}><Pressable accessibilityRole="button" accessibilityLabel={`Inspect ${item.title}`} onPress={() => onPick(item.id)}>
        <Text style={[s.body, matches(item, query) && s.link]}>{matches(item, query) ? '● ' : ''}{item.title}</Text>
        <Text style={s.dim}>{item.state ? `${item.state} · ` : ''}{count(item.size, 'total connection')}</Text></Pressable>
        <View style={s.buttons}>{linked(view, item.id).slice(0, 6).map((n) => <Pressable key={n.id} accessibilityRole="button"
          accessibilityLabel={`From ${item.title} to ${n.title}`} onPress={() => onPick(n.id)}><Text style={s.link}>↗ {n.title}</Text></Pressable>)}</View>
      </View>} />;
}
