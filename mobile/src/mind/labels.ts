import type { MindNode } from '../chat/types';
import type { Point } from './layout';

export const LABEL_FONT = 12;
/** Width of one character at LABEL_FONT, measured generously so labels never touch. */
const CHAR_W = 6.6;
const LABEL_H = LABEL_FONT + 6;
const MAX_LABELS = 36;
const MAX_CHARS = 26;

/** A title short enough to label a dot: dates out (the slider shows them), long tails cut
 * at a word with an ellipsis. The sheet and the list keep the full title. */
export function shortTitle(title: string, max = MAX_CHARS) {
  let t = title.replace(/(^|\D)\d{4}-\d{2}-\d{2}(?!\d)/g, '$1 ').replace(/[\s_–—-]+$/u, '')
    .replace(/^[\s_–—-]+/u, '').replace(/\s{2,}/g, ' ').trim();
  if (!t) t = title.trim();
  if (t.length <= max) return t;
  const cut = t.slice(0, max - 1), space = cut.lastIndexOf(' ');
  return (space > max / 2 ? cut.slice(0, space) : cut).replace(/[\s,;:(–—-]+$/u, '') + '…';
}

/** `side` says which side of its dot the label sits on. */
export type Label = { id: string; text: string; dim: boolean; side: 'right' | 'left' | 'below' | 'above' };

/** Greedy placement at one zoom level: the most connected notes (and search matches,
 * first) claim space beside their dot, on the right or else on the left. A label that would
 * cover another label or a well-connected note is left out, as is a title already shown (a
 * daily series is labelled once), so labels never pile up. Translation does not change
 * overlaps, so the result depends only on the scale. Notes with no connection, letters and
 * conversations are labelled only when zoomed in far enough to be read on their own. */
export function placeLabels(points: Point[], nodes: Pick<MindNode, 'id' | 'title' | 'size'>[], scale: number,
  { query = '', limit = MAX_LABELS }: { query?: string; limit?: number } = {}): Label[] {
  const byId = new Map(points.map((p) => [p.id, p])), sizes = new Map(nodes.map((n) => [n.id, n.size]));
  const q = query.trim().toLocaleLowerCase();
  const found = (n: { title: string }) => !!q && n.title.toLocaleLowerCase().includes(q);
  // Letters and conversations are many and alike: like notes with no connection, they are
  // labelled once zoomed in, or when they match a search.
  const quiet = (n: { id: string; size: number }) => n.size === 0 || n.id.startsWith('letters/') || n.id.startsWith('conversations/');
  const order = [...nodes].filter((n) => byId.has(n.id) && (!quiet(n) || found(n) || scale >= 1.6))
    .sort((a, b) => Number(found(b)) - Number(found(a)) || b.size - a.size || a.id.localeCompare(b.id));
  let left = Infinity, right = -Infinity;
  for (const p of points) { left = Math.min(left, p.x - p.r); right = Math.max(right, p.x + p.r); }
  const boxes: number[][] = [], out: Label[] = [], shown = new Set<string>();
  const hit = (box: number[]) => boxes.some((b) => box[0] < b[2] && b[0] < box[2] && box[1] < b[3] && b[1] < box[3]);
  // A label may cover smaller dots, never a note at least as connected as its own (and
  // with a few connections): the biggest dot of a cluster speaks for it.
  const covers = (box: number[], own: string, size: number) => points.some((p) => p.id !== own
    && (sizes.get(p.id) ?? 0) >= Math.max(3, size)
    && p.x + p.r > box[0] && p.x - p.r < box[2] && p.y + p.r > box[1] && p.y - p.r < box[3]);
  for (const n of order) {
    if (out.length >= limit) break;
    // A series differs only by date: compare whole titles without dates, never the cut ones.
    const p = byId.get(n.id)!, text = shortTitle(n.title), same = shortTitle(n.title, Infinity);
    if (shown.has(same) && !found(n)) continue;
    const w = (text.length * CHAR_W + 4) / scale, h = LABEL_H / scale, gap = 3 / scale, y0 = p.y - h / 2;
    const below = p.y + p.r + gap, above = p.y - p.r - gap - h;
    const sides: [Label['side'], number[]][] = [['right', [p.x + p.r + gap, y0, p.x + p.r + gap + w, y0 + h]],
      ['left', [p.x - p.r - gap - w, y0, p.x - p.r - gap, y0 + h]],
      ['below', [p.x - w / 2, below, p.x + w / 2, below + h]], ['above', [p.x - w / 2, above, p.x + w / 2, above + h]]];
    const chosen = sides.find(([, b]) => b[2] <= right && b[0] >= left && !hit(b) && !covers(b, n.id, n.size));
    if (!chosen) continue;
    const [side, box] = chosen;
    boxes.push(box); shown.add(same);
    out.push({ id: n.id, text, dim: n.size < 3 && !found(n), side });
  }
  return out;
}

/** Labels are placed once per zoom step (about a third of a doubling), not every frame. */
export function zoomStep(scale: number) { return Math.round(Math.log2(Math.max(.01, scale)) * 3); }
export function stepScale(step: number) { return 2 ** (step / 3); }
