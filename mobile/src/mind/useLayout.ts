import { useFocusEffect } from 'expo-router';
import { useCallback, useMemo, useState } from 'react';
import type { MindView } from '../chat/types';
import { advanceLayout, cachedLayout, createLayout, graphKey, saveLayout, type Point } from './layout';

/** A frame is yielded before every bounded chunk (8 ms: half a frame, so the screen stays
 * smooth while it finishes in about a third of the frames 2 ms took). Blur/unmount cancels
 * pending work. */
export function useLayout(view: MindView) {
  const key = useMemo(() => graphKey(view.nodes, view.edges), [view.nodes, view.edges]);
  const [state, setState] = useState<{ key: string; points: Point[]; ready: boolean } | null>(null);
  useFocusEffect(useCallback(() => {
    const saved = cachedLayout(key);
    if (saved) { setState({ key, points: saved, ready: true }); return; }
    const layout = createLayout(view.nodes, view.edges);
    setState({ key, points: layout.points.map((p) => ({ ...p })), ready: layout.done });
    let alive = true, frame: number;
    const run = () => {
      if (!alive) return;
      advanceLayout(layout, 8);
      if (layout.done) {
        saveLayout(key, layout.points);
        setState({ key, points: layout.points.map((p) => ({ ...p })), ready: true });
      } else frame = requestAnimationFrame(run);
    };
    frame = requestAnimationFrame(run);
    return () => { alive = false; cancelAnimationFrame(frame); };
  }, [key, view.nodes, view.edges]));
  return state?.key === key ? state : null;
}
