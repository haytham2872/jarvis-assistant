import { render, screen } from '@testing-library/react-native';
import { Platform, Text } from 'react-native';
import { ReadList } from './ReadList';

it('keeps native virtualization but makes every web fallback row reachable', async () => {
  const data = Array.from({ length: 80 }, (_, i) => `Observation ${i}`);
  const props = { data, renderItem: ({ item }: { item: string }) => <Text>{item}</Text>, initialNumToRender: 6 };
  const native = await render(<ReadList {...props} />);
  expect(screen.getByText('Observation 0')).toBeTruthy(); expect(screen.queryByText('Observation 79')).toBeNull(); await native.unmount();
  const old = Platform.OS;
  Object.defineProperty(Platform, 'OS', { value: 'web', configurable: true });
  try { await render(<ReadList {...props} />); expect(screen.getByText('Observation 79')).toBeTruthy(); }
  finally { Object.defineProperty(Platform, 'OS', { value: old, configurable: true }); }
});
