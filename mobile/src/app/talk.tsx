// exp://…/--/talk: open the chat straight into recording, when Expo Go forwards the link (on a
// cold start). The Action Button's reliable route is the "Ask JARVIS" Shortcut, which calls
// POST /ask on the server directly (Settings explains it): in M0 Expo Go never forwarded a
// link to an app that was already open.
import { Redirect } from 'expo-router';

export default function Talk() {
  return <Redirect href={{ pathname: '/', params: { record: '1' } }} />;
}
