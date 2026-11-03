// A failure on the portfolio screens in one sentence for the owner.
import { HttpError } from '../chat/client';
import { describeFailure, ExplainedError } from '../core/api';

export function explainHolding(e: unknown): string {
  if (e instanceof ExplainedError) return e.message;
  if (e instanceof HttpError && e.status === 404 && /not in your portfolio/.test(e.message)) return e.message;
  if (e instanceof HttpError && e.status === 404) return "This server doesn't have the holding view yet. Update it and restart JARVIS.";
  if (e instanceof HttpError && (e.status === 409 || e.status === 400 || e.status === 503)) return e.message;
  const err = e as { status?: number; message?: string };
  return describeFailure(typeof err?.status === 'number' ? err.status : null, err?.message ?? '');
}
