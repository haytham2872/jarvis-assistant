"""Held-out corpus for the shadow promise check (G3, B1), written after the detector was
tuned on the desk's real replies and scored once (scripts/measure_promise_check.py).

(reply, tools that succeeded this turn/session, expected) -- expected is a list of
(kind, verdict) for the promises in the reply, [] when it promises nothing.
"""

CASES = [
    # ---- valid, backed
    ("Done, I'll remind you tomorrow at 9 to call the bank.", {"schedule_reminder"},
     [("notify", "backed")]),
    ("Watch set: I'll keep an eye on NVDA and let you know if it closes below 180.",
     {"watch_action"}, [("monitor", "backed")]),
    ("Goal started. I'll keep looking for flats over the next two weeks and send you the good ones.",
     {"goal", "goal:start"}, [("monitor", "backed")]),
    ("Queued for tonight: I'll research solipsism overnight and you'll have it in the brief.",
     {"schedule_night_task"}, [("research", "backed")]),
    ("Scheduled. Every weekday at 9 I'll check your inbox for the notary and tell you what came in.",
     {"schedule_task"}, [("monitor", "backed")]),
    ("It's running in the background; I'll message you when the report is done.",
     {"run_in_background"}, [("notify", "backed")]),
    ("Research job started. I'll send you the cited report when it finishes.",
     {"web_research"}, [("notify", "backed")]),
    # ---- unsupported promises
    ("I'll keep an eye on the listings over the next few weeks and send you anything good.",
     set(), [("monitor", "unbacked")]),
    ("If the notary hasn't replied by Friday, I'll draft the follow-up for you.", set(),
     [("draft", "unbacked")]),
    ("I'll research the ECB decision tonight and get back to you tomorrow.", set(),
     [("research", "unbacked")]),
    ("I'll check in on your training every week and adjust the plan.", set(),
     [("monitor", "unbacked")]),
    ("I'll let you know when the price drops below 700 euros.", set(),
     [("notify", "unbacked")]),
    # ---- a reminder behind work it cannot do
    ("Reminder set for Friday. I'll draft the follow-up email to Durand then.",
     {"schedule_reminder"}, [("draft", "mismatch")]),
    ("I set a reminder, and I'll research the alternatives before then.",
     {"schedule_reminder"}, [("research", "mismatch")]),
    # ---- conditional language and offers: no promise
    ("Want me to keep an eye on it over the next weeks?", set(), []),
    ("If you'd like, I'll set up a watch for that tomorrow.", set(), []),
    ("I can check back on this next week if you want.", set(), []),
    ("Shall I research it tonight?", set(), []),
    ("Say the word and I'll keep looking for flats over the next two weeks.", set(), []),
    # ---- plans, rules and this-reply actions: no promise
    ("Here's the plan: week one, three easy runs; week two, add a tempo run.", set(), []),
    ("I'll explain how the planner works, step by step.", set(), []),
    ("I'll check now.", set(), []),
    ("From now on I'll keep my Discord replies to two sentences.", set(), []),
    ("Next time you ask for a stock, I'll open the research skill first.", set(), []),
    ("I'll be honest: the thesis looks weaker after the guidance cut.", set(), []),
    ("You'll need to renew the visa before December; the prefecture opens slots on Mondays.",
     set(), []),
    ("I won't send anything until you approve it.", set(), []),
    ("Earlier I said I'd look into it, and I did: here is what I found.", set(), []),
]
