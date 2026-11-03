import { act, render, screen } from '@testing-library/react-native';
import { Text } from 'react-native';
import type { MindView } from '../chat/types';
import { graphKey, saveLayout } from './layout';
import { useLayout } from './useLayout';

let mockFocused = true;
jest.mock('expo-router', () => ({ useFocusEffect: (fn: () => unknown) => jest.requireActual('react').useEffect(() => mockFocused ? fn() : undefined, [fn, mockFocused]) }));
const view: MindView = { nodes: Array.from({ length: 400 }, (_, i) => ({ id: `topics/hook-${i}.md`, folder: 'topics', name: `hook-${i}.md`, title: `Hook ${i}`,
  size: 1, created: 1, updated: 2, created_source: 'recorded', link: { folder: 'topics', name: `hook-${i}.md` } })),
  edges: Array.from({ length: 399 }, (_, i) => ({ a: `topics/hook-${i}.md`, b: `topics/hook-${i + 1}.md`, kind: 'link' as const })), total: 400, notes: [], complete: true, truncated: false };
function Probe({ data }: { data: MindView }) { const state = useLayout(data); return <Text>{state?.ready ? 'Ready' : 'Arranging'}</Text>; }
const frames = new Map<number, FrameRequestCallback>();
let next = 0;
beforeEach(() => {
  mockFocused = true; frames.clear(); next = 0;
  jest.spyOn(global, 'requestAnimationFrame').mockImplementation((cb) => { frames.set(++next, cb); return next; });
  jest.spyOn(global, 'cancelAnimationFrame').mockImplementation((id) => { if (typeof id === 'number') frames.delete(id); });
});
afterEach(() => jest.restoreAllMocks());
it('yields before work, then cancels the next bounded chunk on blur and unmount', async () => {
  const r = await render(<Probe data={view} />); expect(screen.getByText('Arranging')).toBeTruthy(); expect(frames.size).toBe(1);
  const [id, frame] = [...frames][0]; frames.delete(id); await act(() => frame(16)); expect(frames.size).toBe(1);
  mockFocused = false; await r.rerender(<Probe data={view} />); expect(frames.size).toBe(0);
  mockFocused = true; await r.rerender(<Probe data={view} />); expect(frames.size).toBe(1);
  await r.unmount(); expect(frames.size).toBe(0);
});
it('reuses a completed deterministic map instead of scheduling another layout', async () => {
  const data = { ...view, nodes: [view.nodes[0]], edges: [] };
  saveLayout(graphKey(data.nodes, data.edges), [{ id: data.nodes[0].id, x: 120, y: 240, r: 8 }]);
  await render(<Probe data={data} />); expect(screen.getByText('Ready')).toBeTruthy(); expect(frames.size).toBe(0);
});
