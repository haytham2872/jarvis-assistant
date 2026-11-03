// The map is drawn only once its layout has settled: the unsettled positions are not a map.
import { render, screen } from '@testing-library/react-native';

import { MindMap } from './MindMap';

let mockLayout: { key: string; points: { id: string }[]; ready: boolean } | null = null;
jest.mock('./useLayout', () => ({ useLayout: () => mockLayout }));
jest.mock('../ui/skia', () => ({ skiaAvailable: () => true, SkiaBoundary: ({ children }: { children: unknown }) => children }));
jest.mock('./SkiaMindMap', () => {
  const { Text: MockText } = jest.requireActual('react-native');
  return { SkiaMindMap: (p: { points: unknown[] }) => <MockText>drawn {p.points.length}</MockText> };
});

const view = { nodes: [{ id: 'topics/a.md', folder: 'topics', name: 'a.md', title: 'A', size: 1, created: 1, updated: 2,
  created_source: 'recorded', link: { folder: 'topics', name: 'a.md' } }], edges: [], total: 1, truncated: false, complete: true, notes: [] };
const props = { view: view as never, hidden: [], until: 10, query: '', reduced: false, onPick: jest.fn() };

it('says it is arranging the notes and draws nothing until the layout is ready', async () => {
  mockLayout = { key: 'k', points: [{ id: 'topics/a.md' }], ready: false };
  await render(<MindMap {...props} />);
  expect(screen.getByText(/Arranging his notes/)).toBeTruthy();
  expect(screen.queryByText(/drawn/)).toBeNull();
});

it('draws the map once the layout is ready', async () => {
  mockLayout = { key: 'k', points: [{ id: 'topics/a.md' }], ready: true };
  await render(<MindMap {...props} />);
  expect(screen.getByText('drawn 1')).toBeTruthy();
  expect(screen.queryByText(/Arranging his notes/)).toBeNull();
});
