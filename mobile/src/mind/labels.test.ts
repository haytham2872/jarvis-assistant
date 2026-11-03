import { createLayout, fit } from './layout';
import { placeLabels, shortTitle, stepScale, zoomStep } from './labels';

it('shortens titles: dates out, long tails cut at a word', () => {
  expect(shortTitle('SpaceX (SPCX) — Stock Research Snapshot 2026-10-12')).toBe('SpaceX (SPCX) — Stock…');
  expect(shortTitle('2026-10-12_spcx-stock-research')).toBe('spcx-stock-research');
  expect(shortTitle('NVIDIA close 2026-09-14')).toBe('NVIDIA close');
  expect(shortTitle('Sam')).toBe('Sam');
  expect(shortTitle('2026-10-12')).toBe('2026-10-12'); // nothing else to show
});

function world(n = 240) {
  const nodes = Array.from({ length: n }, (_, i) => ({ id: `${i < 120 ? 'topics' : 'letters'}/n-${String(i).padStart(3, '0')}.md`,
    title: i % 5 ? `A rather long research title number ${i}` : `Series title 2026-10-${String(i % 28 + 1).padStart(2, '0')}`, size: 0 }));
  const edges = nodes.slice(1, 150).map((nd, i) => ({ a: nodes[Math.floor(i / 4)].id, b: nd.id, kind: 'link' as const }));
  const deg = new Map<string, number>();
  for (const e of edges) for (const id of [e.a, e.b]) deg.set(id, (deg.get(id) ?? 0) + 1);
  for (const nd of nodes) nd.size = deg.get(nd.id) ?? 0;
  const layout = createLayout(nodes, edges); while (!layout.done) layout.step(4096);
  return { nodes, points: layout.points };
}
const boxOf = (points: ReturnType<typeof world>['points'], l: { id: string; text: string; side: string }, scale: number) => {
  const p = points.find((q) => q.id === l.id)!, w = (l.text.length * 6.6 + 4) / scale, h = 18 / scale, g = 3 / scale;
  return l.side === 'right' ? [p.x + p.r + g, p.y - h / 2, p.x + p.r + g + w, p.y + h / 2]
    : l.side === 'left' ? [p.x - p.r - g - w, p.y - h / 2, p.x - p.r - g, p.y + h / 2]
    : l.side === 'below' ? [p.x - w / 2, p.y + p.r + g, p.x + w / 2, p.y + p.r + g + h] : [p.x - w / 2, p.y - p.r - g - h, p.x + w / 2, p.y - p.r - g];
};

it.each([.5, 1, 2])('never lets two labels overlap or leave the map, at scale %s', (scale) => {
  const { nodes, points } = world(), labels = placeLabels(points, nodes, scale);
  expect(labels.length).toBeGreaterThan(3);
  const left = Math.min(...points.map((p) => p.x - p.r)), right = Math.max(...points.map((p) => p.x + p.r));
  const boxes = labels.map((l) => boxOf(points, l, scale));
  boxes.forEach((a, i) => {
    expect(a[0]).toBeGreaterThanOrEqual(left - 1e-9); expect(a[2]).toBeLessThanOrEqual(right + 1e-9);
    boxes.slice(i + 1).forEach((b) => expect(a[0] < b[2] && b[0] < a[2] && a[1] < b[3] && b[1] < a[3]).toBe(false));
  });
  expect(labels.filter((l) => l.text.startsWith('Series title')).length).toBeLessThanOrEqual(1); // a daily series is labelled once
});

it('labels the most connected first, leaves quiet notes for zooming in, and puts matches first', () => {
  const { nodes, points } = world();
  const far = placeLabels(points, nodes, .5);
  expect(nodes.find((n) => n.id === far[0].id)!.size).toBe(Math.max(...nodes.map((n) => n.size)));
  expect(far.some((l) => l.id.startsWith('letters/') || nodes.find((n) => n.id === l.id)!.size === 0)).toBe(false);
  const near = placeLabels(points, nodes, 3, { limit: 400 });
  expect(near.some((l) => nodes.find((n) => n.id === l.id)!.size === 0)).toBe(true);
  const quiet = nodes.find((n) => n.size === 0)!;
  expect(placeLabels(points, nodes, .5, { query: quiet.title.slice(-12) })[0].id).toBe(quiet.id);
});

it('places labels once per zoom step', () => {
  expect(zoomStep(1)).toBe(0); expect(zoomStep(1.1)).toBe(0); expect(zoomStep(2)).toBe(3);
  expect(stepScale(zoomStep(.5))).toBeCloseTo(.5);
  const { points } = world(), t = fit(390, 520, points);
  for (const p of points) {
    expect(p.x * t.scale + t.x).toBeGreaterThanOrEqual(0); expect(p.x * t.scale + t.x).toBeLessThanOrEqual(390);
    expect(p.y * t.scale + t.y).toBeGreaterThanOrEqual(0); expect(p.y * t.scale + t.y).toBeLessThanOrEqual(520);
  }
});
