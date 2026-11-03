import { router } from 'expo-router';
import type { ReactNode } from 'react';
import { Text, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import { styles as s } from '../goals/styles';
import { Backdrop } from './Backdrop';
import { IconButton } from './IconButton';

/** Fixed navigation above a virtualized reader; never nests its list in a ScrollView. */
export function ReadFrame({ title, children }: { title: string; children: ReactNode }) {
  const insets = useSafeAreaInsets();
  return <View style={[s.screen, { paddingTop: insets.top }]}><Backdrop />
    <View style={s.header}><IconButton symbol="chevron.left" label="Back" onPress={() => router.canGoBack() ? router.back() : router.replace('/activity')} />
      <Text style={s.title}>{title}</Text></View>{children}</View>;
}
