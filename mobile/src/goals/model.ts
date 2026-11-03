import type { GoalCard, GoalDetail, GoalMilestone, GoalState, HubView } from '../chat/types';

const rank: Record<GoalState, number> = { active: 0, waiting: 1, paused: 2, done: 3, partial: 3, cancelled: 3, failed: 3, proposed: 4 };
export const live = (state: GoalState) => ['active', 'waiting', 'paused'].includes(state);
export const pollMs = (state?: GoalState) => state === 'active' ? 5_000 : 60_000;
export const dollars = (value: number | null) => value === null ? 'Unavailable' : `$${value.toFixed(3)}`;
export const budgetFraction = (spent: number | null, budget: number) =>
  spent === null || !Number.isFinite(spent) || !Number.isFinite(budget) || budget <= 0 ? 0 : Math.max(0, Math.min(1, spent / budget));
export function sortGoals(goals: GoalCard[]) { return [...goals].sort((a, b) => rank[a.state] - rank[b.state] || b.created_at - a.created_at); }
export function nowLine(goal: Pick<GoalCard, 'now' | 'waiting'>) {
  return goal.now || (goal.waiting.length ? `Waiting: ${goal.waiting[0].label}` : 'No session running.');
}
export function deadline(ts: number | null) {
  return ts ? `Due ${new Date(ts * 1000).toLocaleDateString('en-GB', { day: 'numeric', month: 'short' })}` : 'No deadline';
}
export function nodeState(milestone: GoalMilestone, goal: GoalDetail) {
  if (milestone.state === 'done') return 'done';
  if (milestone.state === 'failed') return 'failed';
  if (goal.waiting.some((w) => w.milestone_id === milestone.id)) return 'waiting';
  if (milestone.state === 'working' && goal.state === 'active') return 'working';
  return milestone.state === 'blocked' || milestone.state === 'working' ||
    milestone.depends.some((id) => !goal.milestones.some((m) => m.id === id && m.state === 'done')) ? 'waiting' : 'ready';
}
export function hubLine(goals: HubView['goals']) {
  if (!goals) return 'Open goals';
  if (!goals.enabled) return 'Off on this server';
  return `${goals.active} active · ${goals.questions} ${goals.questions === 1 ? 'question' : 'questions'}`
    + (goals.proposals ? ` · ${goals.proposals} offered` : '');
}
export function linkedEvidence(text: string, ids: string[]) {
  const known = new Set(ids);
  return text.replace(/\[(ev[A-Za-z0-9_-]+)\]/g, (all, id: string) => known.has(id) ? `[${id}](jarvis-evidence:${id})` : all);
}
export function validBudget(raw: string, current: number): number | null {
  if (!/^\d+([.,]\d{1,6})?$/.test(raw.trim())) return null;
  const value = Number(raw.trim().replace(',', '.'));
  return Number.isFinite(value) && value > current && value <= 20 ? value : null;
}
