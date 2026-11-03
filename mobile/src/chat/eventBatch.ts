import type { HubEvent } from './types';

/** One render per burst. Control events drain the queue first, so narration stays before
 * its tool and the final answer never overtakes its last words. */
export function eventBatch(deliver: (events: HubEvent[]) => void) {
  let pending: HubEvent[] = [];
  let timer: ReturnType<typeof setTimeout> | undefined;
  const flush = () => {
    if (timer !== undefined) clearTimeout(timer);
    timer = undefined;
    if (!pending.length) return;
    const events = pending;
    pending = [];
    deliver(events);
  };
  return {
    flush,
    push(evt: HubEvent) {
      if (evt.type !== 'response' && evt.type !== 'thinking') {
        flush();
        deliver([evt]);
        return;
      }
      // Keep sequence numbers intact: snapshots and reconnect replays are deduped by the
      // reducer, including a replay that straddles the last committed sequence number.
      pending.push(evt);
      if (timer === undefined) timer = setTimeout(flush, 50);
    },
  };
}
