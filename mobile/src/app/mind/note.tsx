import { useLocalSearchParams } from 'expo-router';
import { useMemo } from 'react';
import { ActivityIndicator, Text, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import { getVaultNote } from '../../chat/client';
import { MarkdownBlock } from '../../chat/Markdown';
import type { VaultNote } from '../../chat/types';
import { useCachedView, viewKey } from '../../core/useCachedView';
import { Button } from '../../goals/Screen';
import { styles as s } from '../../goals/styles';
import { noteBlocks, noteTitle } from '../../mind/noteModel';
import { dateTime, explain } from '../../overnight/model';
import { useApp } from '../../state/AppContext';
import { color, space } from '../../theme/tokens';
import { ReadFrame } from '../../ui/ReadFrame';
import { ReadList } from '../../ui/ReadList';

export default function NoteScreen() {
  const { cfg } = useApp();
  const params = useLocalSearchParams<{ folder: string; name: string }>();
  const folder = typeof params.folder === 'string' ? params.folder : '', name = typeof params.name === 'string' ? params.name : '';
  const key = viewKey(cfg, `note:${folder}/${name}`);
  const { data, error, stale, loaded, reload } = useCachedView<VaultNote>(key,
    (cache) => getVaultNote(cfg!, folder, name, cache), { refreshMs: 60_000 });
  const blocks = useMemo(() => noteBlocks(data?.text ?? ''), [data?.text]);
  const insets = useSafeAreaInsets();
  return <ReadFrame title="His notes">
    <ReadList key={key} testID="note-blocks" data={blocks} keyExtractor={(_, i) => String(i)} initialNumToRender={6} maxToRenderPerBatch={4} windowSize={5}
      contentContainerStyle={{ padding: space.s4, gap: space.s3, paddingBottom: insets.bottom + space.s6 }}
      refreshing={false} onRefresh={() => void reload(true)}
      ListHeaderComponent={<View style={{ gap: space.s3, marginBottom: space.s3 }}>
        {!cfg ? <Text style={s.dim}>Pair with JARVIS in Settings first.</Text> : null}
        {error ? <><Text style={s.error} accessibilityRole="alert">{explain(error, 'note')}</Text><Button label="Try again" onPress={() => void reload(true)} /></> : null}
        {stale ? <Text style={s.dim}>{stale}</Text> : null}
        {cfg && !data && !loaded ? <ActivityIndicator color={color.stIdle} accessibilityLabel="Loading note" /> : null}
        {data ? <><Text style={s.dim}>{data.folder} / {data.name}</Text>
          {!/^# /m.test(data.text) ? <Text style={s.title}>{noteTitle(data.text, data.name)}</Text> : null}
          <Text style={s.dim}>Modified {dateTime(data.modified)} · Paris</Text>
          {data.truncated ? <Text style={s.dim}>Showing the first 60,000 characters.</Text> : null}
          {!data.text.trim() ? <Text style={s.dim}>This note is empty.</Text> : null}</> : null}
      </View>}
      renderItem={({ item }) => <MarkdownBlock token={item} />} />
  </ReadFrame>;
}
