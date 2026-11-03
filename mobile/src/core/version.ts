// Which published update the phone is running (shown in Settings). Expo Go starts the copy it
// has and downloads a new update in the background, so a fresh one shows only after the next
// cold start (15 October 2026); this line says which one is in front of him.
type Manifest = { id?: string; createdAt?: string } | null | undefined;

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

export function describeUpdate(manifest: Manifest, dev: boolean): string {
  if (dev) return 'Development build from the PC';
  if (!manifest?.createdAt) return 'Update unknown';
  const at = new Date(manifest.createdAt);
  if (Number.isNaN(at.getTime())) return 'Update unknown';
  const hh = String(at.getHours()).padStart(2, '0');
  const mm = String(at.getMinutes()).padStart(2, '0');
  const id = manifest.id ? ` (${manifest.id.slice(0, 8)})` : '';
  return `Update published ${at.getDate()} ${MONTHS[at.getMonth()]}, ${hh}:${mm}${id}`;
}
