"""Local construction and wire-size measurements, with exact owner renderers and no workers."""
import gzip
import json
from statistics import median
from time import perf_counter

import pytest

from backend.brain import positions, skills
from backend.mobile import mind as M
from backend.mobile.test_mind import note, profile


@pytest.mark.parametrize("n", [330, 400])
def test_real_sized_vault_measurement(n):
    M.clear_cache()
    for i in range(95 + n - 330):
        note("topics", f"topic-{i}.md", f"---\nlast_researched: 2026-10-17\nlast_angle: deepen\n---\n# Topic {i}\n\n## Perspectives & Counterpoints\nWill Turner distinguishes evidence from confidence. [[topics/topic-{(i + 1) % 95}]]\n\n## Open questions\nWhat would change the conclusion?\n")
    for name in ("Will Turner", "Anna Cole", "Karim Haddad", "Rose Miller", "Jean Moreau"):
        profile(name)
    note("people", "owner.md", "# Sam\n\nHe asks what the evidence can actually support.")
    note("people", "_template.md", "# Template"); note("people", "predictions.md", "# Predictions")
    for i in range(4):
        note("skills", f"procedure-{i}.md", skills.render(skills.Skill(name=f"procedure-{i}", description="Check primary sources before summarizing", body="# Check sources\n\nRead [[topics/topic-0]].", created="2026-09-12")))
    from datetime import date, timedelta
    for folder, count in (("letters", 143), ("conversations/significant", 65)):
        for i in range(count):
            name = f"{date(2026, 5, 1) + timedelta(days=i)}_{i:08x}.md"
            note(folder, name, f"Dear future self,\n\nThe conversation with Will Turner left a question about [[topics/topic-{i % 95}]]. Keep it open rather than claiming it was resolved.\n")
    for i in range(8): note("creative", f"listening-{i}.md", f"# Listening {i}\n\nA sketch around [[topics/topic-{i}]].")
    ps = {f"topic-{i}": positions.Position(topic=f"Topic {i}", stance="Confidence follows evidence.", sources=f"[[topics/topic-{i}]]", formed="2026-09-21", revisions=[]) for i in range(9)}
    note("self", "positions.md", positions.render_file(ps))
    # 95+6+4+143+65+8+9 = 330; add 70 topics for the cap. All letters have a real link.
    before = perf_counter(); v = M.view(); cold = (perf_counter() - before) * 1000
    timings = []
    for _ in range(20):
        before = perf_counter(); assert M.view() is v; timings.append((perf_counter() - before) * 1000)
    payload = json.dumps(v, separators=(",", ":"), ensure_ascii=False).encode()
    print("MIND_SERVER " + json.dumps({"nodes": len(v["nodes"]), "edges": len(v["edges"]), "coldMs": round(cold, 3),
          "warmMedianMs": round(median(timings), 3), "warmP95Ms": round(sorted(timings)[18], 3), "jsonBytes": len(payload), "gzipBytes": len(gzip.compress(payload))}))
    assert v["complete"] and len(v["nodes"]) == n and not v["truncated"]
    M.clear_cache()
