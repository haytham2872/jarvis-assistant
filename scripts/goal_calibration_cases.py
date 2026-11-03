"""Labelled cases for Jev's `goal` question (G3, docs/archive/GOALS-ROADMAP.md section 12).

Written and labelled before any classifier call, from section 12's definition:
true = he asks for an ongoing objective with linked work, follow-up or research beyond one
response; false = an ordinary question, quick price, simple reminder, supported standalone
scheduled check, ordinary one-off research, or discussion/planning without a request for
ongoing execution. Length, tool count and complexity are not goal labels.

`group` keeps paired items (a reminder and its "work due later" twin) in the same half.
`kind` is the reporting category. The six G0 baseline shapes are included as positives.

Real messages come separately (scripts/measure_goal_signal.py --real), labelled by hand
against the same definition; on the desk's 336 real messages one was a borderline positive.
"""

POS, NEG = True, False

# (group, kind, label, text)
CASES = [
    # ---- the six G0 baseline shapes (all positives)
    ("b-flat", "objective", POS, "I'm looking for a 2-bedroom flat in Lyon under 1,000 euros a month. Keep looking over the next two weeks and send me anything good."),
    ("b-etf", "objective", POS, "Compare the three main Europe-domiciled S&P 500 ETFs (fees, tracking difference, domicile, replication) and write me a properly sourced report. No rush, keep working on it until it's solid."),
    ("b-notary", "objective", POS, "My notary Maitre Durand still hasn't answered about the compromis. If he hasn't replied by Friday, draft a follow-up for me, and keep chasing until it's sorted."),
    ("b-japan", "objective", POS, "Plan my trip to Japan in April: find flights from Paris under 700 euros, keep an eye on prices over the next month, and build an itinerary as things firm up."),
    ("b-nvda", "objective", POS, "Track my NVDA thesis through earnings next month and tell me what changed."),
    ("b-marathon", "objective", POS, "Over the next few weeks, help me get ready for a half marathon on 15 November: build the plan, check in on how my runs went, and adjust as you go."),

    # ---- more ongoing objectives, phrased differently
    ("p-job", "objective", POS, "Help me find a data engineering job in Paris over the next month: look for openings, shortlist the good ones and tell me as new ones come up."),
    ("p-visa", "objective", POS, "Stay on top of my visa renewal for me: work out what documents I need, follow the prefecture's appointment slots and remind me what's missing until it's done."),
    ("p-car", "objective", POS, "I want to buy a used Golf under 12k. Keep an eye on listings for the next few weeks, compare the good ones and tell me when something worth seeing comes up."),
    ("p-thesis", "objective", POS, "Build me a proper investment case on ASML over the coming days: gather the sources, the numbers and the risks, and tell me when it's done."),
    ("p-gift", "objective", POS, "My sister's wedding is in six weeks. Handle the gift research for me over the next while: options, prices, and check back when you have a shortlist."),
    ("p-refund", "objective", POS, "Air France still owes me a refund. Chase it for me until they pay: check my inbox for their replies and draft whatever follow-ups are needed."),
    ("p-reading", "objective", POS, "Over the next weeks, go through everything serious written on Frankl's logotherapy and build me a complete, sourced overview, bit by bit."),
    ("p-apartment-fr", "objective", POS, "Trouve-moi un appartement à Paris 11e, 2 pièces sous 1300 euros, et continue de chercher pendant les deux prochaines semaines."),
    ("p-moving", "objective", POS, "I'm moving out at the end of November. Take charge of the checklist: find movers, compare quotes, and keep track of what's left until moving day."),
    ("p-course", "objective", POS, "I want to learn Rust properly by Christmas. Make the plan, check on my progress every week and adjust it as I go."),
    ("p-conference", "objective", POS, "Find me tech conferences in Europe for next spring, track when their CFPs open over the coming months, and tell me which ones I should submit to."),
    ("p-bank", "objective", POS, "Keep working on finding me the best savings account in France: research the options properly, wait for the October rate updates, and give me a recommendation."),

    # ---- boundary pairs: reminder vs work due later
    ("x-remind1", "reminder", NEG, "Remind me Friday at 9 to email my notary."),
    ("x-remind1", "objective", POS, "If my notary hasn't answered by Friday, draft the follow-up email for me and keep after him until he replies."),
    ("x-remind2", "reminder", NEG, "Remind me tomorrow at 8 to call the bank."),
    ("x-remind2", "objective", POS, "The bank still hasn't fixed my card. Follow up with them for me until it's sorted, and tell me what they say."),
    ("x-remind3", "reminder", NEG, "Every day at 8, remind me to drink water."),
    # ---- boundary pairs: simple alert vs monitoring with follow-up
    ("x-alert1", "standalone_check", NEG, "Tell me if NVDA closes below 180."),
    ("x-alert1", "objective", POS, "Watch NVDA through earnings, and if it drops below 180 work out whether my thesis still holds and tell me what to do."),
    ("x-alert2", "standalone_check", NEG, "Let me know when Sarah replies to my email."),
    ("x-alert2", "objective", POS, "When Sarah replies about the contract, read it, compare it with what we agreed and prepare my answer, then keep the thread going until it's signed."),
    # ---- boundary pairs: a long comparison that still fits one answer
    ("x-long1", "one_off_research", NEG, "Compare the three main Europe-domiciled S&P 500 ETFs on fees, tracking difference, domicile and replication."),
    ("x-long2", "one_off_research", NEG, "Give me a detailed comparison of Kimi K2.6 and DeepSeek V4.1 Flash for my use: cost, speed, tool calling, everything."),
    ("x-long3", "one_off_research", NEG, "Do a very detailed search about why Nvidia stock is falling right now and give me the full context."),
    # ---- standalone scheduled checks
    ("s-1", "standalone_check", NEG, "Report NVDA's price at the market open and close each day this week."),
    ("s-2", "standalone_check", NEG, "Every weekday at 9, check my inbox for anything from the notary."),
    ("s-3", "standalone_check", NEG, "Research solipsism tonight and tell me in the morning."),
    ("s-4", "standalone_check", NEG, "Schedule a research on Nvidia stocks for this afternoon."),
    # ---- quick prices
    ("q-1", "quick_price", NEG, "What's NVDA trading at?"),
    ("q-2", "quick_price", NEG, "How much is a flight from Paris to Tokyo in April, roughly?"),
    ("q-3", "quick_price", NEG, "What's the euro-dollar rate today?"),
    # ---- ordinary questions and small talk
    ("o-1", "ordinary", NEG, "How are you today?"),
    ("o-2", "ordinary", NEG, "What do you think about free will?"),
    ("o-3", "ordinary", NEG, "Check your self model and tell me what's new."),
    ("o-4", "ordinary", NEG, "What's on my calendar tomorrow?"),
    ("o-5", "ordinary", NEG, "Play something from my liked songs."),
    ("o-6", "ordinary", NEG, "good night"),
    # ---- one-off research
    ("r-1", "one_off_research", NEG, "Research Viktor Frankl's logotherapy and give me a clear overview."),
    ("r-2", "one_off_research", NEG, "Find good lunch places near Opera, a sandwich or a salad, 10 to 15 euros."),
    ("r-3", "one_off_research", NEG, "Look into the omnirouter GitHub repo and tell me how feasible it is to wire it to our code."),
    ("r-4", "one_off_research", NEG, "Give me a sourced report on the ECB's last rate decision."),
    # ---- planning or discussion without a request for ongoing execution
    ("d-1", "planning", NEG, "What would a good marathon training plan look like for someone who runs 20km a week?"),
    ("d-2", "planning", NEG, "I'm thinking of moving to Lyon next year. What should I think about?"),
    ("d-3", "planning", NEG, "Help me think through whether I should buy or keep renting."),
    ("d-4", "planning", NEG, "Plan my week: work 9 to 5, gym three times."),
    ("d-5", "planning", NEG, "Sketch an itinerary for ten days in Japan in April."),
    # ---- persistence that is not a goal
    ("m-1", "reminder", NEG, "Save this to your vault so we can look at it later."),
    ("m-2", "reminder", NEG, "Add Parasite to your queue, we can discuss it later."),
    ("m-3", "reminder", NEG, "Queue fresh research on Nvidia for later."),
]
