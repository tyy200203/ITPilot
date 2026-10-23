import json
from pathlib import Path

from core.intent_recognizer import IntentCategory, _TEMPLATES
from evaluation.intent_benchmark import classification_metrics, load_dataset


DATASET_PATH = Path(__file__).parents[1] / "evaluation" / "datasets" / "intent-benchmark-v1.json"


def test_intent_benchmark_is_balanced_complete_and_has_no_template_leakage():
    metadata, cases = load_dataset(DATASET_PATH)
    labels = [case["expected_intent"] for case in cases]
    messages = [case["message"] for case in cases]
    expected_labels = {intent.value for intent in IntentCategory}
    template_messages = {message for values in _TEMPLATES.values() for message in values}

    assert metadata["suite_id"] == "itpilot-intent-benchmark-v1"
    assert len(cases) == 190
    assert set(labels) == expected_labels
    assert all(labels.count(label) == 10 for label in expected_labels)
    assert len(messages) == len(set(messages))
    assert template_messages.isdisjoint(messages)


def test_classification_metrics_calculate_accuracy_macro_f1_and_support():
    metrics = classification_metrics(
        ["network_issue", "network_issue", "device_failure"],
        ["network_issue", "device_failure", "device_failure"],
    )

    assert metrics["total"] == 3
    assert metrics["correct"] == 2
    assert metrics["accuracy"] == 0.6667
    assert metrics["macro_f1"] == 0.6667
    assert metrics["per_class"]["network_issue"]["support"] == 2
