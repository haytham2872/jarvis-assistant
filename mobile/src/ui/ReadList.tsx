import { Fragment, type ReactElement } from 'react';
import { FlatList, type FlatListProps, Platform, RefreshControl, ScrollView } from 'react-native';
import { color } from '../theme/tokens';

type Props<T> = Omit<FlatListProps<T>, 'data' | 'renderItem' | 'ListHeaderComponent'> & {
  data: readonly T[]; renderItem: (info: { item: T; index: number }) => ReactElement | null;
  ListHeaderComponent?: ReactElement;
};

/** Expo Go keeps bounded native cells. RN Web 0.21 can stall after its initial window;
 * the browser fallback renders the finite, server-capped document without that scheduler. */
export function ReadList<T>(props: Props<T>) {
  if (Platform.OS !== 'web') return <FlatList {...props} />;
  const { data, renderItem, keyExtractor, ListHeaderComponent, contentContainerStyle, style, testID, onRefresh, refreshing } = props;
  return <ScrollView testID={testID} style={style} contentContainerStyle={contentContainerStyle}
    refreshControl={onRefresh ? <RefreshControl refreshing={refreshing ?? false} onRefresh={onRefresh} tintColor={color.stIdle} /> : undefined}>
    {ListHeaderComponent}
    {data.map((item, index) => <Fragment key={keyExtractor?.(item, index) ?? String(index)}>{renderItem({ item, index })}</Fragment>)}
  </ScrollView>;
}
