// The share sheet's Shortcut (slice 7) talks to /ask directly, like the Action Button's.
import { endpoint } from '../core/api';

/** /ask with the file named "shared" (the server names it from its bytes) and "q=" last, so the
 *  Shortcut appends his URL-encoded question. */
export const shareAddress = (baseUrl: string) => `${endpoint(baseUrl, '/ask')}?name=shared&q=`;
