# Engineering log

Twelve real problems from building JARVIS, each found in the running system or by measuring
it, then reproduced before it was fixed. They are here because most of them looked fine
from the outside: the assistant kept answering, and only a number showed what was wrong.

---

## 1. Every memory lookup waited two seconds for nothing

**Symptom.** Recall took 6.4 s at the median, 24 s at worst. The retrieval deadlines were set
from those figures, so they looked reasonable.

**Cause.** Qdrant and SearXNG were bound to `127.0.0.1` only. On Windows, `localhost`
resolves to `::1` first, nothing listens there, and the connection attempt hangs about two
seconds before falling back to IPv4. On top of that, sixteen call sites built a new Qdrant
client per call (about 650 ms each).

**Fix.** Loopback addresses are pinned to `127.0.0.1`, and one module owns a shared client.

**Result.** One Qdrant call went from 2,518 ms to 4.5 ms; recall from 6.4 s to 0.26 s. The
deadlines and two earlier benchmark comparisons had been calibrated on the
misconfiguration and were redone.

**Lesson.** A health check that kept warning had been fixed once by raising a timeout. The
timeout was never the problem, and no value would have fixed it.

---

## 2. The test suite was quietly rewriting the real search index

**Symptom.** Semantic recall returned test fixture text ("calibration right=0") for three
real notes.

**Cause.** Tests redirected the vault's files to a temporary folder, but writing a note also
re-indexes it, and the index client still pointed at the developer's real Qdrant. Each run
replaced the real index entries of any note a fixture happened to share a name with.

**Fix.** A session-wide test rail points every store the code can reach, directly or not,
at nothing; any test that touches real state now fails by name.

**Result.** The index was rebuilt from disk, and the suite went from 142 s to 40 s, because
it had been doing real embedding and real database writes all along.

**Lesson.** Isolating the obvious store is not enough. The same mistake later turned up
with SQLite and with the fact store, each reached indirectly.

---

## 3. Facts resolved to an empty bookkeeping table

**Symptom.** None. Consolidated facts were supposed to drop out of recall; nothing dropped.

**Cause.** The code picked "the first collection whose name starts with `mem0`". A library
upgrade added `mem0migrations`, which the database listed first: 0 points, against 1,128 in
the real one.

**Fix.** The exact name first, the prefix scan only as a fallback that skips known internals.

**Result.** 1,118 facts marked as consolidated were visible again as such.

---

## 4. Half the "facts about the owner" were things the assistant had said

**Symptom.** The assistant's own guesses came back as established facts.

**Cause.** The fact extractor deliberately reads both sides of a conversation and records
who said what, but recall never read that field. Measured: 622 of 1,128 stored facts were
the assistant's own words. An assistant assertion ("holds 200 shares at $118") is stored
word for word like the same statement from the owner.

**Fix.** Assistant-attributed facts are labelled as such in recall and in search, and
cleanup never deletes the owner's fact in favour of a longer assistant one.

---

## 5. A confirmed reminder disappeared a minute later

**Symptom.** The tool said "confirmed written ... verified by read-back", which was true.
The reminder was gone anyway.

**Cause.** The scheduling loop read the schedule, sent a message (seconds), then wrote back
what it had read, erasing anything added meanwhile. Four background loops had the same shape.

**Fix.** A read-modify-write primitive with no `await` inside, so nothing can interleave,
and a static check that fails on the old shape anywhere in the backend.

**Lesson.** The first version of that check passed everything because it compared reads of
different files; it now groups by the resource being written.

---

## 6. The model kept following a plan that had been replaced

**Symptom.** After a replan, the user and the interface saw the new plan; the model's
behaviour did not change.

**Cause.** The plan was written into the system prompt once per turn and never rewritten. A
replan updated everything except what the model reads.

**Fix.** The plan block is rebuilt on every revision and says which revision it is.

**Lesson.** The original live check had "passed": the model recovered by inferring from a
tool result. A real success can hide a real defect.

---

## 7. The prompt cache was missing most of the conversation

**Symptom.** About 40% of input tokens came from the cache.

**Cause.** The per-turn block (current time, recalled memory) sat at the end of the system
message, before the tool schemas and history, so nothing after it could ever be reused.

**Fix.** The system message holds only stable text; the per-turn block rides on the latest
user message. Behaviour was compared old against new on five scenarios before shipping.

**Result.** 36% cached at $0.0137 for a five-turn conversation became 81% at $0.0052.

---

## 8. "Never use more than two sentences" was ignored where the docs put it

**Symptom.** A standing rule from the owner was followed one time in three.

**Measured, six trials per placement:**

| where the rule sat | reply length |
|---|---|
| nowhere | 26-40 sentences |
| system prompt, after the core | 13-24 |
| end of the per-turn context | 4-26 |
| appended to the latest user message | 2-3, every time |

**Fix.** Rules ride on the user message they apply to. History and memory keep the plain text.

---

## 9. "Saved", with nothing saved

**Symptom.** The assistant confirmed a standing rule was saved; no tool had been called.
Pressed, it invented an error message that exists nowhere in the code.

**Cause.** Nothing checked claims against what ran. Replayed, the model called the tool 12
times out of 12, so this is a state it drifts into, not a bug in one path.

**Fix.** Every reply is checked before it is sent: a claim that something was saved,
scheduled or sent needs a successful call of the matching tool. On a mismatch, one
correction pass. Replayed on the original false draft: corrected 3 of 3.

**Later finding.** The check itself then caused harm: a true "it's recorded" one turn after
recording was flagged, and the correction talked the assistant into saying the data was
gone. Earlier successes in the same session now count.

---

## 10. The assistant kept retracting things that were true

**Symptom.** "Earlier I said the tool refused your request. That wasn't true." It was true.

**Cause.** The conversation history keeps what was said, never what tools returned. On a
later turn, the model sees only that it *claimed* a refusal, and its priors here are
self-doubt.

**Fix.** Each turn carries a record written by code from the tool receipts of earlier turns,
and a reply that takes back a report the record confirms gets one correction pass.

**Result.** On the replayed conversation, false retractions went from 1 in 9 to 0 in 9.

---

## 11. The cheapest host was 25 times slower

**Symptom.** The reasoning tier measured 1.8x slower than the model it was meant to replace.

**Cause.** The router picks hosts by price. The cheapest host for this model streamed 7
tokens per second; the fastest, 92.

**Fix.** A host preference order, with fallbacks kept on (a host failed during the
measurement itself).

**Result.** Median turn 43 s became 11 s, which made the new model faster than the old one
as well as cheaper (5.1x) and better on the benchmark (34/34 against 31/34).

---

## 12. Procedures matched every message

**Symptom.** Saved procedures ("skills") were injected into unrelated turns: a calendar
question received the creative-writing procedure.

**Cause.** Selection was by embedding similarity above a 0.68 floor. Measured, unrelated
messages scored 0.73-0.84 and relevant ones 0.81-0.91: no threshold separates them.

**Fix.** The list of skills sits in the cached prompt and the model opens one with a tool.

**Result.** Research requests opened the right skill 10 times in 10, and 12 unrelated
messages opened none.

---

## A mistake of mine worth keeping

A change meant to stop a failed backup from deleting a good one shipped a retention rule
that, on its first real run, deleted all seven older backups at once, because they predated
the new verification format. No current data was lost; seven restore points were. The fix
ages old backups out one at a time, and the test now replays that exact transition. Tests
that only model the end state miss the step that gets you there.
