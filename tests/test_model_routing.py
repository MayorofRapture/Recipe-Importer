import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from model_routing import (
    ModelConfig,
    ModelRouter,
    QuotaExhausted,
    UsageLedger,
    parse_model_config,
)


class ModelRoutingTests(unittest.TestCase):
    def test_rpm_windows_are_shared_by_independent_routers_and_survive_restart(self):
        class Clock:
            current = datetime(2026, 10, 15, tzinfo=timezone.utc)

            def now(self):
                return self.current

            def sleep(self, seconds):
                self.current += timedelta(seconds=seconds)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "usage.json"
            clock = Clock()
            starts = []

            for index, model in enumerate(("shared", "shared", "other", "shared")):
                model_rpm = 2 if model == "shared" and index == 0 else 12
                router = ModelRouter(
                    [ModelConfig(model, rpm=model_rpm)],
                    UsageLedger(path, now=clock.now),
                    global_rpm=4 if index == 0 else 12, sleep=clock.sleep,
                )
                router.run(lambda selected: starts.append((selected.id, clock.now())) or "ok")

        self.assertEqual([name for name, _ in starts], ["shared", "shared", "other", "shared"])
        self.assertEqual([int((when - starts[0][1]).total_seconds()) for _, when in starts], [0, 30, 45, 60])

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
