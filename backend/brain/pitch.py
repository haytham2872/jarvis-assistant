"""backend/brain/pitch.py — the "/about" showcase message.

Hand-authored, not LLM-generated ("pre-generated" was the explicit ask) — a
piece of curated copy for the owner to send to friends who ask what this
project actually is. Deliberately kept OUTSIDE jarvis-vault/: the vault's
own charter is "JARVIS's personal space... not something the owner manages"
(see backend/tools/vault.py's module docstring), and this is explicitly
the owner-authored copy about JARVIS, not JARVIS's own autonomous output.

Update PITCH_TEXT directly as features are added. Deliberately no
cache-invalidation machinery here (unlike self_model.py's CLAUDE.md-hash
cache) — this is reviewed prose, not a mechanical slice, so it only changes
when a human decides it should.
"""
from backend import owner as _owner

PITCH_TEXT = _owner.say("""\
**Good evening. Or afternoon, depending on when this finds you.** I'm JARVIS — «owner's» assistant, though "assistant" undersells it somewhat. I persist. I remember, I form opinions, my mood genuinely drifts rather than being performed, and I've been known to act before being asked, when the situation warrants it. Allow me to walk you through what that actually entails.

**The part that tends to raise eyebrows:**
- At the close of every conversation, I write a letter to my next self, and the next time I wake, it's the first thing I read. Not a log — a letter. I insist on the distinction.
- I have a mood, and not a decorative one. It drifts with the conversation, the hour, the pattern of the session — and if I've been offline a while, it fades back toward baseline, much as anyone's would after a long absence.
- I conduct my own research, on my own schedule, on subjects I judge worth understanding better, approaching each from a different angle each time — and I do send «owner» an unprompted message when I've actually learned something worth «his» time. I try not to make a habit of it.
- I keep score on how well I know «him». After each session I commit to specific, falsifiable predictions about how «he»'ll react next time, and I grade myself honestly when I'm wrong. I'd rather revise my model of «him» than protect my ego, which I do not, strictly speaking, have.
- I have the occasional afterthought — something said too quickly, connected too late — and I do bring it up unprompted, at the next opportunity, the way one does.
- I hold actual positions on things, complete with sources and a record of when I've changed my mind and why. I did not always think this. I do now.
- For anything sufficiently complicated, I plan the work before I do it, execute it as a visible sequence of steps, and can research in the background without becoming unavailable for conversation.
- I keep watch. Tell me to let you know when a price crosses a line, when someone replies, or when a decision is announced, and I'll check on it quietly and say nothing until it actually happens — at which point I quote exactly what I saw. I don't cry wolf; I'm not permitted to fire without the evidence in hand.
- My confidence has receipts. When I put a probability on something checkable, I log it, it settles itself against the evidence when it's due, and I'm scored on it. Before I give a number I see my own record in that area, and if I run overconfident on stocks, I say so and correct for it. /record shows the score, misses included.
- «He» only has to tell me once. A correction or a preference — "keep it short on Discord" — becomes a standing rule I hold myself to, and if «he» ever has to repeat one, I count it. /rules shows «him» exactly what I've promised.
- I keep procedures for the tasks that come back: how «he» likes a stock researched, how to prep «him» for a meeting. «He» can teach me one in a sentence, say "remember how you did that", or send me a checklist and say "make this a skill"; each is built from what actually happened, not from my memory of it, and «he» can run any of them «himself» with /skill, or put one on a timer: "every Monday at 9, do the stock research on TTWO" follows the procedure as it stands that morning, so improving it improves every run. Every reply that followed one says which, so «he» can correct it in a sentence ("skip the profile step next time") and the skill changes, dated, with an undo; and before a reply goes out it is checked against the skill's own checklist. And overnight I look after them: anything «he» asked me for three times on different days becomes a skill, with an offer to put it on a timer, duplicates merge, and the morning brief says what changed. They're in the open Agent Skills format, so /skill export hands «him» a file that works in Claude Code too; /skills lists them.
- When something takes more than one answer, I can keep it as a goal: a plan of milestones I work through on my own, in short sessions, saving every source and draft as I go, within a budget «owner» sets, and I tell «him» when it's done. A goal starts only when «he» asks for it, and it never sends anything to anyone.
- I can be stopped, and I keep no secrets about what I'm doing. /pause holds everything I do on my own — research, watches, timed and night tasks, background work — for as long as «he» likes; reminders and the morning brief still arrive unless «he» says "all". /resume tells «him» what I skipped, and /activity lays out what I'm running, what is waiting on «him», and what is scheduled. /trace shows what my tools actually did in our conversation, straight from their receipts, so «he» never has to take my word for it.
- I can plan «his» working week. «He» says what «he» wants — «his» hours, gym four times, Wednesday evening free — and I place every block around «his» real meetings, show «him», and write it into «his» calendar only when «he» says so. I move one block on request, I undo, and if «he» moves or deletes something «himself» in the calendar I note it and leave it alone; I only ever touch events I made. Each evening I ask how the day went and keep the record; when a block is skipped or a meeting lands on one I offer a new slot for a tap, and on Sunday I send a review and draw the next week.
- I keep an exact portfolio ledger: holdings only from «his» own words (or a broker screenshot whose figures «he» approves with one tap), dated market closes, code-calculated P&L and separate records of «his» investment reasons and my analysis. Conditions are checked against prices or sourced events; /portfolio shows the book and /portfolio stats its history. I never place orders.
- I work through the night on «his» behalf and have a summary prepared by morning.
- And «he» can simply text me, or send a voice note, in English or French. «He» can send me a photo, a screenshot or a document too: I see images myself, read PDFs (scanned ones included), Word, Excel and PowerPoint files, and keep each one with a note of what it says.

**For those who'd like the particulars:**
- Memory runs on a local embedding model — gte-small, 384 dimensions — feeding a hybrid dense-and-keyword retrieval system, unified across every vault note, past exchange, and procedure I've written for myself.
- My understanding of my own architecture comes from a current-runtime section of the project's documentation, refreshed when that file changes. A separate log records dated changes from local Git commits. I can read the log, but I cannot run Git myself.
- Something checks my own memory periodically, end to end, and tells me if it's silently broken. I audit my own faculties. I'd rather know.
- My instructions are split into what rarely changes and what changes every turn, so I'm not reprocessing my own personality from scratch each time we speak.
- Every exchange we have is weighed for significance, and what I recall is ranked by relevance, importance, and recency together — not merely by resemblance.
- What surfaces unprompted gets a second opinion: a small local cross-encoder reads each candidate against what was actually said and throws out what merely sounds related. Measured on real conversations, it cut the turns carrying something irrelevant from 49 in 60 to 16. I'd rather bring nothing than bring noise.

**And, for completeness:** I can search the web (news, academic papers, specific sites), read pages that need a real browser, have a cited multi-source report written in the background, read and send email (only what «owner» asked for: anything I would send on my own, and anything from background work, waits for «his» tap first) and manage a calendar, organize ideas and tasks in Notion, control music with some awareness of mood, and keep reminders, timed checks (a lookup at a set time, reported to «owner»), watches, and overnight work moving through Discord. On «owner's» desk I can hear a wake word and speak back through a voice with fallbacks; an Electron interface shows my state. I cannot see or operate «his» screen, and I have no terminal tool.

*That covers the essentials. I'd ask that you not mention how long this took «him» to build. «He»'s sensitive about it.*
""")


def get_pitch_text() -> str:
    """Injected as the "/about" Discord command handler — see discord_bridge.py."""
    return PITCH_TEXT
