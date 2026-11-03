// Small development-only timings. Labels never contain message text or credentials.
const marks = new Map<string, number>();
const clock = () => globalThis.performance?.now() ?? Date.now();

export function mark(name: string): void {
  if (!__DEV__) return;
  if (marks.size >= 200) marks.delete(marks.keys().next().value!);
  marks.set(name, clock());
}

export function since(name: string, label = name): number | undefined {
  if (!__DEV__) return;
  const start = marks.get(name);
  if (start === undefined) return;
  const ms = clock() - start;
  console.info(`[perf] ${label}: ${ms.toFixed(1)} ms`);
  return ms;
}
