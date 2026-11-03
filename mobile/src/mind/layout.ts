import type { MindEdge, MindNode } from '../chat/types';

export type Point = { id: string; x: number; y: number; r: number };
export type Transform = { x: number; y: number; scale: number };
type Node = Pick<MindNode, 'id' | 'size'>;
export const WORLD = 1000;
const ITERATIONS = 300;
const CENTER = WORLD / 2;
/** Spacing of the ring that holds notes with no connection, in world units. */
const RING_GAP = 15;

function hash(id: string) {
  let h = 2166136261;
  for (let i = 0; i < id.length; i++) h = Math.imul(h ^ id.charCodeAt(i), 16777619);
  return h >>> 0;
}
export function graphKey(nodes: Node[], edges: MindEdge[]) {
  return JSON.stringify([nodes.map((n) => [n.id, n.size]).sort(), edges.map((e) => [e.a, e.b].sort()).sort()]);
}
/** A connected note grows with its connections; a note with none is a small dot. */
export function radius(size: number) { return size > 0 ? 4 + Math.min(12, Math.sqrt(size) * 1.8) : 3.5; }

/** Incremental Fruchterman–Reingold with grid repulsion over the connected notes only.
 * Nearby points in the same cell repel exactly; other cells repel through their mass and
 * centroid, and gravity keeps separate clusters together. Notes with no connection would
 * be pushed to the edges by every other note, so they are left out of the forces and set
 * on a ring around the constellation once it has settled, banded by folder.
 * Work order depends only on ids, never on frame timing or the input array order. */
export function createLayout(nodes: Node[], edges: MindEdge[]) {
  const points: Point[] = [...nodes].sort((a, b) => a.id.localeCompare(b.id)).map((n) => ({ id: n.id,
    // The axis goes first: FNV mixes a last character only once, and 'x' and 'y' differ by one
    // bit, so id + ':x' and id + ':y' gave every note y = x +- 2.3 -- the map opened as a line.
    x: 200 + hash('x:' + n.id) / 0xffffffff * 600, y: 200 + hash('y:' + n.id) / 0xffffffff * 600, r: radius(n.size) }));
  const index = new Map(points.map((p, i) => [p.id, i]));
  const pairs = edges.flatMap((e) => {
    const a = index.get(e.a), b = index.get(e.b);
    return a === undefined || b === undefined || a === b ? [] : [[Math.min(a, b), Math.max(a, b)]];
  });
  const linked = new Set(pairs.flat());
  const active = points.map((_, i) => i).filter((i) => linked.has(i));     // connected, in id order
  const loose = points.map((_, i) => i).filter((i) => !linked.has(i));
  const slot = new Map(active.map((p, i) => [p, i]));
  const links = pairs.map(([a, b]) => [slot.get(a)!, slot.get(b)!]).sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  const n = active.length;
  const dx = new Float64Array(n), dy = new Float64Array(n);
  // Ideal spacing: shrinks as the map fills, capped so a small map does not drift apart.
  const k = Math.min(60, Math.sqrt(420 * 420 / Math.max(1, n)));
  const gravity = 2.4;
  const cells = Array.from({ length: 25 }, () => ({ x: 0, y: 0, members: [] as number[] }));
  // The grid spans the notes' current extent, not the working square: a settled map is
  // compact, and fixed cells would put most notes in a few cells compared pair by pair.
  let gx = 0, gy = 0, gw = WORLD, gh = WORLD, nx0 = Infinity, ny0 = Infinity, nx1 = -Infinity, ny1 = -Infinity;
  const cellOf = new Int32Array(n);
  // Resolve Math once: repeated global lookups are expensive in both worklets and Jest VMs.
  const { max, min, sqrt, hypot, floor, cos, sin, PI, ceil } = Math;
  let iteration = n < 2 ? ITERATIONS : 0, phase = 0, i = 0, cell = 0, member = 0, placed = false;
  const at = (j: number) => points[active[j]];
  const repel = (node: number, x: number, y: number, count: number) => {
    let vx = at(node).x - x, vy = at(node).y - y;
    if (vx === 0 && vy === 0) { vx = .01; vy = .01; }
    const force = k * k * count / max(1, vx * vx + vy * vy);
    dx[node] += vx * force; dy[node] += vy * force;
  };
  /** The ring: every loose note at its own angle, alternating between rings, so folder
   * bands (ids start with the folder) read as arcs of one colour. */
  const placeLoose = () => {
    let reach = 0;
    for (const j of active) reach = max(reach, hypot(points[j].x - CENTER, points[j].y - CENTER) + points[j].r);
    const inner = active.length ? reach + 34 : 40;
    const rings = max(1, ceil(loose.length * RING_GAP / (2 * PI * inner)));
    loose.forEach((j, rank) => {
      const angle = -PI / 2 + 2 * PI * rank / max(1, loose.length), ring = inner + (rank % rings) * RING_GAP;
      points[j].x = CENTER + cos(angle) * ring; points[j].y = CENTER + sin(angle) * ring;
    });
    placed = true;
  };
  // Nothing to settle: the ring is final at once.
  if (n < 2) placeLoose();
  return {
    points,
    get done() { return placed; },
    get progress() { return placed ? 1 : iteration / ITERATIONS; },
    step(work = 128) {
      while (work-- > 0 && !placed) {
        if (iteration >= ITERATIONS) { placeLoose(); break; }
        if (phase === 0) {
          const p = at(i), index = min(4, max(0, floor((p.x - gx) / gw * 5))) + min(4, max(0, floor((p.y - gy) / gh * 5))) * 5;
          const c = cells[index]; c.x += p.x; c.y += p.y; c.members.push(i); cellOf[i] = index;
          if (++i === n) { phase = 1; i = 0; cell = 0; member = 0; }
        } else if (phase === 1) {
          const c = cells[cell];
          if (cellOf[i] === cell && member < c.members.length) {
            const other = c.members[member++];
            if (other !== i) {
              repel(i, at(other).x, at(other).y, 1);
              // Close neighbours also keep their discs apart, so a dense series of notes
              // reads as separate dots instead of one blob.
              const vx = at(i).x - at(other).x, vy = at(i).y - at(other).y, d = hypot(vx, vy) || .01;
              const need = at(i).r + at(other).r + 6;
              if (d < need) { dx[i] += vx / d * (need - d) * k; dy[i] += vy / d * (need - d) * k; }
            }
          } else {
            if (cellOf[i] !== cell && c.members.length) repel(i, c.x / c.members.length, c.y / c.members.length, c.members.length);
            member = 0;
            if (++cell === cells.length) { cell = 0; i++; }
            if (i === n) { phase = 2; i = 0; }
          }
        } else if (phase === 2) {
          if (i < links.length) {
            const [a, b] = links[i++], x = at(a).x - at(b).x, y = at(a).y - at(b).y;
            const force = sqrt(x * x + y * y) / k;
            dx[a] -= x * force; dy[a] -= y * force; dx[b] += x * force; dy[b] += y * force;
          } else { phase = 3; i = 0; }
        } else {
          const p = at(i);
          dx[i] += (CENTER - p.x) * gravity; dy[i] += (CENTER - p.y) * gravity;
          const distance = max(.001, hypot(dx[i], dy[i]));
          const travel = min(distance, 12 * (1 - iteration / ITERATIONS) + .02);
          p.x = max(5, min(WORLD - 5, p.x + dx[i] / distance * travel));
          p.y = max(5, min(WORLD - 5, p.y + dy[i] / distance * travel));
          nx0 = min(nx0, p.x); ny0 = min(ny0, p.y); nx1 = max(nx1, p.x); ny1 = max(ny1, p.y);
          dx[i] = 0; dy[i] = 0; i++;
          if (i === n) {
            iteration++; phase = 0; i = 0;
            gx = nx0; gy = ny0; gw = max(1, nx1 - nx0); gh = max(1, ny1 - ny0);
            nx0 = Infinity; ny0 = Infinity; nx1 = -Infinity; ny1 = -Infinity;
            for (const c of cells) { c.x = 0; c.y = 0; c.members.length = 0; }
          }
        }
      }
    },
  };
}
export type Layout = ReturnType<typeof createLayout>;

/** Called once per animation frame; checks the clock after at most 128 cheap operations. */
export function advanceLayout(layout: Layout, budgetMs = 2, clock = () => performance.now()) {
  const start = clock();
  do { layout.step(); } while (!layout.done && clock() - start < budgetMs);
}
const cache = new Map<string, Point[]>();
export function cachedLayout(key: string) { return cache.get(key); }
export function saveLayout(key: string, points: Point[]) {
  if (cache.size >= 4) cache.delete(cache.keys().next().value!);
  cache.set(key, points.map((p) => ({ ...p })));
}

export function inverse(point: { x: number; y: number }, transform: Transform) {
  'worklet';
  return { x: (point.x - transform.x) / transform.scale, y: (point.y - transform.y) / transform.scale };
}
export function hitTest(points: Point[], screen: { x: number; y: number }, transform: Transform) {
  const p = inverse(screen, transform);
  let nearest: Point | null = null, best = Infinity;
  for (const node of points) {
    const distance = Math.hypot(node.x - p.x, node.y - p.y);
    if (distance <= Math.max(node.r, 18 / transform.scale) && distance < best) { best = distance; nearest = node; }
  }
  return nearest?.id ?? null;
}
/** Fits the notes themselves, not the layout's working square, with room for a label. */
export function fit(width: number, height: number, points: Point[] = []): Transform {
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const p of points) { x0 = Math.min(x0, p.x - p.r); y0 = Math.min(y0, p.y - p.r); x1 = Math.max(x1, p.x + p.r); y1 = Math.max(y1, p.y + p.r); }
  if (!points.length) { x0 = 0; y0 = 0; x1 = WORLD; y1 = WORLD; }
  const pad = 14, w = Math.max(40, x1 - x0), h = Math.max(40, y1 - y0);
  const scale = Math.max(.05, Math.min((width - 2 * pad) / w, (height - 2 * pad) / h, 3));
  return { x: (width - w * scale) / 2 - x0 * scale, y: (height - h * scale) / 2 - y0 * scale, scale };
}
export function pinch(start: Transform, focal: { x: number; y: number }, scale: number): Transform {
  'worklet';
  const next = Math.max(.15, Math.min(6, start.scale * scale));
  const anchor = inverse(focal, start);
  return { x: focal.x - anchor.x * next, y: focal.y - anchor.y * next, scale: next };
}
