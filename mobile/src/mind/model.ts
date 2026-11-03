import type { MindNode, MindView } from '../chat/types';
import { color } from '../theme/tokens';

export const folders: Record<string, { label: string; color: string }> = {
  topics: { label: 'Topics', color: color.stIdle }, people: { label: 'People', color: color.warn },
  self: { label: 'Positions', color: color.stThinking }, skills: { label: 'Skills', color: color.ok },
  letters: { label: 'Letters', color: '#b5c9e2' }, creative: { label: 'Creative', color: '#e69bbd' },
  'conversations/significant': { label: 'Conversations', color: '#7ad6cf' }, goals: { label: 'Goal results', color: '#d1c37d' },
};
export const folderLabel = (folder: string) => folders[folder]?.label ?? folder;
export const count = (n: number, label: string) => `${n} ${label}${n === 1 ? '' : 's'}`;
export function visibleGraph(view: MindView, hidden: string[], until: number) {
  const nodes = view.nodes.filter((n) => !hidden.includes(n.folder) && n.created <= until);
  const ids = new Set(nodes.map((n) => n.id));
  return { nodes, edges: view.edges.filter((e) => ids.has(e.a) && ids.has(e.b)) };
}
export function linked(view: MindView, id: string) {
  const ids = new Set(view.edges.flatMap((e) => e.a === id ? [e.b] : e.b === id ? [e.a] : []));
  return view.nodes.filter((n) => ids.has(n.id)).sort((a, b) => a.title.localeCompare(b.title));
}
export function matches(node: MindNode, query: string) { return !!query.trim() && node.title.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()); }
export function glow(updated: number, at: number, reduced: boolean) {
  return reduced || updated > at ? 0 : Math.max(0, Math.min(1, 1 - (at - updated) / (14 * 86400)));
}
export function mapDate(at: number) { return new Date(at * 1000).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' }); }
export function hubMind(value?: { count: number; total: number; complete: boolean }) {
  if (!value) return 'Waiting for the map';
  return `${count(value.count, 'note')}${value.count < value.total ? ` of ${value.total}` : ''}${value.complete ? '' : ' · partial map'}`;
}
export function timeAt(x: number, width: number, start: number, end: number) {
  'worklet';
  return start + Math.max(0, Math.min(1, x / Math.max(1, width))) * (end - start);
}
