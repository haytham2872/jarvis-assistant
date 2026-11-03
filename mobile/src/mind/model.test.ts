import type { MindNode, MindView } from '../chat/types';
import { folderLabel, glow, hubMind, linked, mapDate, matches, timeAt, visibleGraph } from './model';

const node = (id: string, created: number): MindNode => ({ id, folder: id.split('/')[0], name: id.split('/')[1], title: id,
  created, updated: created + 1, created_source: 'filename', size: 1, link: { folder: id.split('/')[0], name: id.split('/')[1] } });
const view: MindView = { nodes: [node('topics/Duration', 100), node('people/Will', 200)], edges: [{ a: 'topics/Duration', b: 'people/Will', kind: 'mention' }], total: 2, complete: true, notes: [], truncated: false };
it('filters nodes and every dangling edge by date and folder', () => {
  expect(visibleGraph(view, [], 150)).toEqual({ nodes: [view.nodes[0]], edges: [] });
  expect(visibleGraph(view, ['people'], 250)).toEqual({ nodes: [view.nodes[0]], edges: [] });
  expect(visibleGraph(view, [], 250).edges).toHaveLength(1);
  expect(linked(view, 'topics/Duration')).toEqual([view.nodes[1]]);
  expect(matches(view.nodes[0], ' duration ')).toBe(true); expect(matches(view.nodes[0], '')).toBe(false);
});
it('turns off glow for Reduce Motion, fades by 14 days, and keeps the date filter usable', () => {
  expect(glow(100, 100, false)).toBe(1); expect(glow(100, 100 + 7 * 86400, false)).toBe(.5);
  expect(glow(100, 100 + 14 * 86400, false)).toBe(0); expect(glow(100, 100, true)).toBe(0);
  expect(glow(200, 100, false)).toBe(0); // a later edit cannot glow in an earlier view
  expect(timeAt(50, 100, 100, 200)).toBe(150); expect(timeAt(-20, 100, 100, 200)).toBe(100);
  expect(timeAt(120, 100, 100, 200)).toBe(200); expect(timeAt(1, 0, 100, 200)).toBe(200);
  expect(visibleGraph(view, [], timeAt(0, 100, 100, 200)).nodes).toHaveLength(1);
});
it('labels partial counts, folders and dates in Paris', () => {
  expect(hubMind({ count: 260, total: 330, complete: false })).toBe('260 notes of 330 · partial map');
  expect(hubMind()).toContain('Waiting'); expect(folderLabel('self')).toBe('Positions');
  expect(mapDate(Date.parse('2026-10-24T23:30Z') / 1000)).toBe('25 Oct 2026');
});
