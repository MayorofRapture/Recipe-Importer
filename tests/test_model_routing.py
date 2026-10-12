import json
import tempfile
import unittest
from pathlib import Path

from model_routing import (
    ModelConfig,
    ModelRouter,
    QuotaExhausted,
    UsageLedger,
    parse_model_config,
)


class ModelRoutingTests(unittest.TestCase):
    def test_parses_ordered_roles_and_limits_from_json(self):
        configs = parse_model_config(json.dumps({"models": [
            {"id": "primary", "role": "primary", "rpm": 10, "rpd": 20},
            {"id": "strong", "role": "quality_escalation", "rpm": 5, "rpd": 10},
            {"id": "fallback", "role": "quota_fallback", "rpm": 5, "rpd": 8},
        ]}))

        self.assertEqual([item.id for item in configs], ["primary", "strong", "fallback"])
        self.assertEqual(configs[1].role, "quality_escalation")
        self.assertEqual(configs[2].rpd, 8)

    def test_primary_success_does_not_escalate(self):
        with tempfile.TemporaryDirectory() as tmp:
            router = ModelRouter([
                ModelConfig("primary", "primary", rpd=10),
                ModelConfig("strong", "quality_escalation", rpd=10),
            ], UsageLedger(Path(tmp) / "usage.json"), sleep=lambda _: None)
            calls = []

            result = router.run(lambda model: calls.append(model.id) or "ok")

        self.assertEqual(result.value, "ok")
        self.assertEqual(calls, ["primary"])

    def test_quality_failure_uses_quality_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            router = ModelRouter([
                ModelConfig("primary", "primary", rpd=10),
                ModelConfig("strong", "quality_escalation", rpd=10),
            ], UsageLedger(Path(tmp) / "usage.json"), sleep=lambda _: None)
            calls = []

            result = router.run(
                lambda model: calls.append(model.id) or {"model": model.id},
                quality_check=lambda value: value["model"] == "primary",
            )

        self.assertEqual(result.value["model"], "strong")
        self.assertEqual(calls, ["primary", "strong"])
        self.assertEqual(result.reason, "quality_escalation")

    def test_quota_failure_uses_fallback_without_repeating_exhausted_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            router = ModelRouter([
                ModelConfig("primary", "primary", rpd=10),
                ModelConfig("fallback", "quota_fallback", rpd=10),
            ], UsageLedger(Path(tmp) / "usage.json"), sleep=lambda _: None)
            calls = []

            def attempt(model):
                calls.append(model.id)
                if model.id == "primary":
                    raise QuotaExhausted("primary daily quota")
                return "fallback-result"

            result = router.run(attempt)

        self.assertEqual(result.value, "fallback-result")
        self.assertEqual(calls, ["primary", "fallback"])
        self.assertEqual(result.reason, "quota_fallback")

    def test_usage_limit_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "usage.json"
            first = UsageLedger(path)
            first.reserve("primary", 1)
            second = UsageLedger(path)

            with self.assertRaises(QuotaExhausted):
                second.reserve("primary", 1)


if __name__ == "__main__":
    unittest.main()
