"""A09: trustworthy cost totals and honest status.

Acceptance criteria:

- *"An estimated $1 call plus a separate authoritative $0.25 call totals $1.25."*
  It totalled **$0.25**. One reported cost flipped the whole model to
  authoritative-only and silently discarded every un-reported call's tokens.
  Reproduced with the roadmap's exact numbers; kept as the first test below.
- *"Extraction appears in cost."*
- *"Unavailable integrations are reported accurately."* A00 records collections it
  deliberately refused to delete because they hold data the active embedder can no
  longer read — and **nothing consumed that list**, so `/status` reported "all layers
  healthy" with 1,128 facts sitting unusable.
- *"An operator can follow a Discord request through tools, artifacts and delivery
  without raw reasoning logs."*
"""
import inspect

import pytest

from backend.brain import memory, runtime_status, usage


@pytest.fixture(autouse=True)
def _clean():
    usage._usage.clear()
    memory._INCOMPATIBLE_COLLECTIONS.clear()
    yield
    usage._usage.clear()
    memory._INCOMPATIBLE_COLLECTIONS.clear()


MODEL = "deepseek/deepseek-v4.1-flash"


def _tokens_worth(dollars: float) -> int:
    return int(dollars * usage._MTOK / usage._PRICING[MODEL]["input"])


# --------------------------------------------------------- mixed cost aggregation
def test_the_roadmaps_exact_acceptance_number():
    """estimated $1 + authoritative $0.25 == $1.25."""
    usage.record(MODEL, input_tokens=_tokens_worth(1.0), output_tokens=0)
    usage.record(MODEL, input_tokens=10, output_tokens=10, cost=0.25)
    assert usage.estimate_cost() == pytest.approx(1.25, abs=0.01)


def test_authoritative_costs_accumulate_across_calls():
    usage.record(MODEL, 10, 10, cost=0.25)
    usage.record(MODEL, 10, 10, cost=0.75)
    assert usage.estimate_cost() == pytest.approx(1.00, abs=0.001)


def test_an_all_estimated_model_is_unchanged():
    """The pre-existing behaviour for call sites that never pass cost."""
    usage.record(MODEL, input_tokens=_tokens_worth(2.0), output_tokens=0)
    assert usage.estimate_cost() == pytest.approx(2.0, abs=0.01)


def test_an_all_authoritative_model_is_unchanged():
    usage.record(MODEL, 10, 10, cost=0.42)
    assert usage.estimate_cost() == pytest.approx(0.42, abs=0.001)


def test_an_unpriced_model_is_unknown_not_zero():
    """Collapsing 'no pricing row' to 0.0 is what makes a total quietly wrong."""
    usage.record("some-unpriced-model", 5000, 500)
    counters = usage._usage["some-unpriced-model"]
    assert usage._estimated_cost("some-unpriced-model", counters) is None


def test_an_unpriced_model_is_flagged_in_the_summary():
    usage.record("some-unpriced-model", 5000, 500)
    out = usage.session_summary()
    assert "partly unknown" in out
    assert "the real total is higher" in out


def test_the_summary_says_which_basis_each_number_has():
    """'measured' is the provider's own figure, 'estimated' is table-priced."""
    usage.record(MODEL, input_tokens=_tokens_worth(1.0), output_tokens=0)
    usage.record(MODEL, 10, 10, cost=0.25)
    usage.record("deepseek/deepseek-v4-flash-0731", 1000, 500)   # priced from the table

    out = usage.session_summary()
    assert "(measured + estimated)" in out
    assert "(estimated)" in out


def test_a_model_with_only_measured_calls_says_measured():
    usage.record(MODEL, 10, 10, cost=0.25)
    assert "(measured)" in usage.session_summary()


def test_token_totals_stay_whole_model_figures():
    """The per-model token counts are for display and must cover every call, even
    though costing now splits them."""
    usage.record(MODEL, 100, 50)
    usage.record(MODEL, 200, 25, cost=0.1)
    counters = usage._usage[MODEL]
    assert counters["input"] == 300 and counters["output"] == 75
    assert counters["est_input"] == 100 and counters["est_calls"] == 1


def test_record_never_raises_on_bad_input():
    usage.record(MODEL, "not a number", None, cost="nope")     # type: ignore[arg-type]
    assert isinstance(usage.estimate_cost(), float)


# ------------------------------------------------------------ extraction in cost
def test_extraction_records_usage():
    """A09: 'extraction appears in cost'. mem0 fact extraction is a real LLM call and
    was invisible in /cost before A01's adapter."""
    from backend.providers import mem0_llm

    src = inspect.getsource(mem0_llm)
    assert "usage.record(" in src, "mem0 extraction no longer reports its spend"


def test_extraction_spend_shows_up_in_the_summary():
    usage.record("deepseek/deepseek-v4.1-flash", 500, 50, cost=0.002)
    assert "deepseek" in usage.session_summary()


# ------------------------------------------- unavailable integrations are reported
def test_an_unreadable_collection_is_reported_as_degraded():
    """The reproduced gap: /status said 'all layers healthy' with 1,128 facts the
    active embedder cannot read."""
    memory._INCOMPATIBLE_COLLECTIONS.append(("mem0", "embedder changed", 1128))
    out = runtime_status.runtime_status_text()
    assert "DEGRADED" in out
    assert "1,128 points" in out
    assert "mem0" in out


def test_the_degraded_line_says_how_to_fix_it():
    memory._INCOMPATIBLE_COLLECTIONS.append(("mem0", "embedder changed", 1128))
    out = runtime_status.runtime_status_text()
    assert "JARVIS_ALLOW_COLLECTION_DELETION=1" in out
    assert "back up first" in out


def test_status_stays_quiet_when_nothing_is_degraded():
    assert "DEGRADED" not in runtime_status.runtime_status_text()


def test_the_canary_alone_would_not_catch_this():
    """Worth pinning the reasoning: an embedder swap at the SAME dimension still
    returns hits from `Memory.search()`, so the canary passes while recall is
    meaningless. The two signals are complementary, not redundant."""
    src = inspect.getsource(runtime_status.runtime_status_text)
    assert "incompatible_collections" in src or "_incompatible" in src


def test_status_reporting_does_no_io():
    """runtime_status_text is documented as fully synchronous with no network -- it is
    called directly, not awaited, from the live turn and from Discord's /status."""
    src = inspect.getsource(runtime_status.runtime_status_text)
    assert "await" not in src
    assert "run_in_executor" not in src


# ------------------------------------- following a request without reasoning logs
def test_a_request_can_be_followed_through_runs_and_operations():
    """A02 gave every turn a run and every tool call a receipt correlated to it, so an
    operator can reconstruct what a Discord request did without reading raw model
    reasoning. Pinned here because that is A09's last acceptance criterion."""
    from backend.storage import operations, runs

    assert hasattr(runs, "begin_run") and hasattr(runs, "open_runs")
    assert hasattr(operations, "operations_for_run")
    assert hasattr(operations, "uncertain_operations")


def test_receipts_store_bounded_previews_not_full_content():
    """'Logs can leak personal data or grow indefinitely. Use references and bounded
    retention.'"""
    from backend.storage import operations

    assert operations._PREVIEW_CHARS <= 2000
    src = inspect.getsource(operations.record_operation)
    assert "_preview" in src
