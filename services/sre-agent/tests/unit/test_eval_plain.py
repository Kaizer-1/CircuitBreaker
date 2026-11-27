"""The evaluation's plain-words check finds evidence field names used as words, and nothing else."""

import json

from eval.plain import field_names, quoted_fields

EVIDENCE = json.dumps(
    {
        "alert": {"name": "GatewayHighErrorRate", "firing_for_seconds": 40},
        "facts": {"passed_on_share": 1.0, "upstream_with_most_errors": "order-service",
                  "errors_per_second_by_type": {"INTERNAL_ERROR": 3.1},
                  "revision": {"new": False, "running_for_minutes": 300}},
        "deploy_history": [{"changed_from_previous": None}],
    }
)  # fmt: skip


def test_the_field_names_of_the_evidence_are_found_at_any_depth() -> None:
    assert field_names(EVIDENCE) == {
        "firing_for_seconds",
        "passed_on_share",
        "upstream_with_most_errors",
        "running_for_minutes",
        "deploy_history",
        "changed_from_previous",
        "errors_per_second_by_type",
    }  # not INTERNAL_ERROR: an error code is the system's vocabulary, not a field name


def test_a_field_name_used_as_a_word_is_caught_plain_words_are_not() -> None:
    names = field_names(EVIDENCE)
    assert quoted_fields("All errors are passed on (passed_on_share 1.0).", names) == [
        "passed_on_share"
    ]
    plain = "All of the gateway's errors come from order-service, which it only passes on."
    assert quoted_fields(plain, names) == []
