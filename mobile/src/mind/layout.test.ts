import { advanceLayout, cachedLayout, createLayout, fit, graphKey, hitTest, inverse, pinch, saveLayout } from './layout';
import type { MindEdge } from '../chat/types';

function fixture(n: number) {
  const nodes = Array.from({ length: n }, (_, i) => ({ id: `${i < 95 ? 'topics' : i < 238 ? 'letters' : 'people'}/note-${i}.md`, size: i % 11 }));
  const edges: MindEdge[] = [];
  const pairs = new Set<string>();
  for (let i = 0; i < n; i++) for (const offset of [1, 7, 19]) {
    if (i % 9 === 0 && offset !== 1) continue; // some thin notes, denser topic clusters
    const a = nodes[i].id, b = nodes[(i + offset) % n].id, key = [a, b].sort().join(':');
    if (!pairs.has(key)) { pairs.add(key); edges.push({ a, b, kind: offset === 7 ? 'mention' : 'link' }); }
  }
  return { nodes, edges };
}
function finish(layout: ReturnType<typeof createLayout>, work: number) {
  while (!layout.done) layout.step(work);
  return layout.points;
}
it('is deterministic across frame boundaries, input order and repeated runs', () => {
  const { nodes, edges } = fixture(25);
  const a = finish(createLayout(nodes, edges), 17), b = finish(createLayout([...nodes].reverse(), [...edges].reverse()), 1024);
  expect(b).toEqual(a); expect(finish(createLayout(nodes, edges), 333)).toEqual(a);
  expect(a.every((p) => Number.isFinite(p.x) && Number.isFinite(p.y))).toBe(true);
  expect(graphKey(nodes, edges)).toBe(graphKey([...nodes].reverse(), [...edges].reverse()));
  saveLayout(graphKey(nodes, edges), a); expect(cachedLayout(graphKey(nodes, edges))).toEqual(a);
});
it('places connected nodes closer than disconnected ones and handles empty or singleton maps', () => {
  const nodes = ['a', 'b', 'c', 'd'].map((id) => ({ id, size: 1 }));
  const [a, b, c] = finish(createLayout(nodes, [{ a: 'a', b: 'b', kind: 'link' }]), 128);
  expect(Math.hypot(a.x - b.x, a.y - b.y)).toBeLessThan(Math.hypot(a.x - c.x, a.y - c.y));
  expect(createLayout([], []).done).toBe(true); expect(createLayout([nodes[0]], []).done).toBe(true);
});
it('hit-tests after pan and zoom through the inverse transform, not at screen coordinates', () => {
  const points = [{ id: 'target', x: 100, y: 200, r: 9 }, { id: 'screen-decoy', x: 360, y: 670, r: 9 }];
  const transform = { scale: 3, x: 60, y: 70 };
  expect(inverse({ x: 360, y: 670 }, transform)).toEqual({ x: 100, y: 200 });
  expect(hitTest(points, { x: 360, y: 670 }, transform)).toBe('target');
  expect(hitTest(points, { x: 100, y: 200 }, transform)).toBeNull();
  const initial = fit(390, 350), anchor = { x: 200, y: 170 };
  expect(inverse(anchor, pinch(initial, anchor, 2))).toEqual(inverse(anchor, initial));
});
it('checks the budget during a pair pass, and resumes without changing the result', () => {
  const g = fixture(15), layout = createLayout(g.nodes, g.edges);
  let now = 0; advanceLayout(layout, 2, () => now++);
  expect(layout.done).toBe(false); expect(layout.progress).toBeLessThan(.1);
  expect(finish(layout, 49)).toEqual(finish(createLayout(g.nodes, g.edges), 811));
});
it.each([330, 400])('measures a %i-node, realistic-edge layout in bounded chunks', (n) => {
  const { nodes, edges } = fixture(n), layout = createLayout(nodes, edges);
  const started = performance.now(), chunks: number[] = [];
  while (!layout.done) { const before = performance.now(); advanceLayout(layout); chunks.push(performance.now() - before); }
  const total = performance.now() - started, sorted = [...chunks].sort((a, b) => a - b);
  console.info(`MIND_LAYOUT ${JSON.stringify({ nodes: n, edges: edges.length, iterations: 300, totalMs: +total.toFixed(2), chunks: chunks.length,
    maxChunkMs: +Math.max(...chunks).toFixed(2), p95ChunkMs: +sorted[Math.floor(sorted.length * .95)].toFixed(2) })}`);
  expect(layout.points).toHaveLength(n);
  expect(sorted[Math.floor(sorted.length * .95)]).toBeLessThan(8);
  // A GC/host scheduling pause can exceed a frame; the algorithm itself polls every 128 operations.
  expect(total).toBeLessThan(14000);
});
it('rings the notes with no connection outside the connected ones, banded by folder, never against walls', () => {
  const nodes = [...Array.from({ length: 30 }, (_, i) => ({ id: `topics/t-${String(i).padStart(2, '0')}.md`, size: i < 20 ? 2 : 0 })),
    ...Array.from({ length: 30 }, (_, i) => ({ id: `letters/l-${String(i).padStart(2, '0')}.md`, size: 0 }))];
  const edges = nodes.slice(0, 19).map((n, i) => ({ a: n.id, b: nodes[i + 1].id, kind: 'link' as const }));
  const points = finish(createLayout(nodes, edges), 256), r = (p: { x: number; y: number }) => Math.hypot(p.x - 500, p.y - 500);
  const linkedReach = Math.max(...points.filter((p) => /topics\/t-[01]/.test(p.id)).map(r)), loose = points.filter((p) => !p.id.startsWith('topics/t-0') && !p.id.startsWith('topics/t-1'));
  expect(loose).toHaveLength(40);
  expect(Math.min(...loose.map(r))).toBeGreaterThan(linkedReach);
  expect(points.every((p) => p.x > 40 && p.x < 960 && p.y > 40 && p.y < 960)).toBe(true);
  const angle = (p: { x: number; y: number }) => (Math.atan2(p.y - 500, p.x - 500) + 2.5 * Math.PI) % (2 * Math.PI);
  const letters = loose.filter((p) => p.id.startsWith('letters/')).map(angle), topics = loose.filter((p) => p.id.startsWith('topics/')).map(angle);
  expect(Math.max(...letters)).toBeLessThan(Math.min(...topics)); // one arc per folder
});
it('fits the notes themselves, whatever their spread', () => {
  const points = [{ id: 'a', x: 400, y: 450, r: 5 }, { id: 'b', x: 600, y: 550, r: 5 }], t = fit(390, 520, points);
  const xs = points.map((p) => p.x * t.scale + t.x);
  expect(Math.min(...xs)).toBeCloseTo(390 - Math.max(...xs), 5); expect(t.scale).toBeGreaterThan(1);
});
it('starts the notes spread out, not on a line', () => {
  // id + ':x' and id + ':y' once hashed to nearly the same number, so every note started at
  // y = x +- 2.3 and the map opened as a diagonal line until the layout settled.
  const { nodes, edges } = fixture(120);
  const p = createLayout(nodes, edges).points;
  const mean = (v: number[]) => v.reduce((s, x) => s + x, 0) / v.length;
  const xs = p.map((q) => q.x), ys = p.map((q) => q.y), mx = mean(xs), my = mean(ys);
  let c = 0, vx = 0, vy = 0;
  p.forEach((_, i) => { c += (xs[i] - mx) * (ys[i] - my); vx += (xs[i] - mx) ** 2; vy += (ys[i] - my) ** 2; });
  expect(Math.abs(c / Math.sqrt(vx * vy))).toBeLessThan(0.3);
  expect(Math.max(...p.map((q) => Math.abs(q.y - q.x)))).toBeGreaterThan(100);
});
