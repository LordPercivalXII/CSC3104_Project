"""Regression cases for useful-output savings, including failed delivery and waste."""
import pytest

from eval.cost.analyze import run_metrics


PRICES = {"llm": [{"input_per_mtok": 1, "output_per_mtok": 2, "thinking_billed_as": "output"}]}


def request(**changes):
    row = {
        "status": 200, "hit": 0, "correct": 1, "confusable_error": 0, "synthetic": 0,
        "llm_calls": 1, "base_prompt_tokens": 100, "base_output_tokens": 50,
        "base_thinking_tokens": 10, "llm_prompt_tokens": 100, "llm_output_tokens": 50,
        "llm_thinking_tokens": 10, "gw_wall_ms": 20, "llm_latency_s": 1,
        "cpu_s": 0.01, "cpu_baseline_s": 0,
    }
    row.update(changes)
    return row


def no_inference(**changes):
    return request(llm_calls=0, llm_prompt_tokens=0, llm_output_tokens=0,
                   llm_thinking_tokens=0, **changes)


def test_failed_zero_call_request_cannot_look_like_a_cache_saving():
    rows = [
        no_inference(hit=1),  # correct reuse
        request(),  # completed miss: replay costs exactly the baseline
        no_inference(hit=1, correct=0, confusable_error=1),  # wrong reuse
        no_inference(status=504, correct=0),  # deadline before inference
    ]
    metrics = run_metrics(rows, PRICES)
    assert metrics["error_rate"] == 0.25
    assert metrics["hit_rate"] == 0.5
    assert metrics["correct_hit_rate"] == 0.25
    assert metrics["wrong_rate"] == 0.25
    assert metrics["llm_calls"] == 1
    assert metrics["llm_calls_avoided"] == 2
    assert metrics["tokens_saved"] == 320  # two delivered hits, no credit for failure
    assert metrics["tokens_saved_frac"] == 0.5  # 320 / full-stream baseline 640
    assert metrics["saved_gross_per_req|0"] == pytest.approx(0.00044 / 4)
    assert metrics["saved_correct_per_req|0"] == pytest.approx(0.00022 / 4)


def test_failed_request_with_actual_spend_is_a_negative_benefit():
    row = request(status=503, correct=0, llm_prompt_tokens=25,
                  llm_output_tokens=7, llm_thinking_tokens=3)
    metrics = run_metrics([row], PRICES)
    assert metrics["error_rate"] == 1
    assert metrics["llm_calls"] == 1
    assert metrics["llm_calls_avoided"] == -1
    assert metrics["tokens_saved"] == -35
    assert metrics["tokens_saved_frac"] == pytest.approx(-35 / 160)
    assert metrics["saved_gross_per_req|0"] == pytest.approx(-0.000045)
    assert metrics["saved_correct_per_req|0"] == pytest.approx(-0.000045)


@pytest.mark.parametrize("status,expected_calls,expected_tokens,expected_money", [
    (200, -1, -160, -0.00022),  # one delivered answer used two calls' tokens
    (504, -2, -320, -0.00044),  # two calls used tokens but no answer was delivered
])
def test_repeated_inference_is_charged_for_successful_and_failed_requests(
        status, expected_calls, expected_tokens, expected_money):
    row = request(status=status, correct=int(status == 200), llm_calls=2,
                  llm_prompt_tokens=200, llm_output_tokens=100, llm_thinking_tokens=20)
    metrics = run_metrics([row], PRICES)
    assert metrics["llm_calls"] == 2
    assert metrics["llm_calls_avoided"] == expected_calls
    assert metrics["tokens_saved"] == expected_tokens
    assert metrics["saved_gross_per_req|0"] == pytest.approx(expected_money)
    assert metrics["saved_correct_per_req|0"] == pytest.approx(expected_money)


def test_wrong_hit_with_actual_spend_is_not_free_in_correct_only_savings():
    metrics = run_metrics([request(hit=1, correct=0)], PRICES)
    assert metrics["saved_gross_per_req|0"] == 0
    assert metrics["saved_correct_per_req|0"] == pytest.approx(-0.00022)


def test_failed_row_cannot_earn_credit_even_if_hit_fields_are_set():
    metrics = run_metrics([no_inference(status=504, hit=1, correct=1)], PRICES)
    assert metrics["hit_rate"] == metrics["correct_hit_rate"] == 0
    assert metrics["llm_calls_avoided"] == metrics["tokens_saved"] == 0
    assert metrics["saved_gross_per_req|0"] == metrics["saved_correct_per_req|0"] == 0


def test_successful_data_retains_the_existing_savings_definition():
    rows = [no_inference(hit=1), request(), no_inference(hit=1, correct=0)]
    metrics = run_metrics(rows, PRICES)
    assert metrics["error_rate"] == 0
    assert metrics["llm_calls_avoided"] == 2
    assert metrics["tokens_saved"] == 320
    assert metrics["tokens_saved_frac"] == pytest.approx(2 / 3)
    assert metrics["saved_gross_per_req|0"] == pytest.approx(0.00044 / 3)
    assert metrics["saved_correct_per_req|0"] == pytest.approx(0.00022 / 3)
