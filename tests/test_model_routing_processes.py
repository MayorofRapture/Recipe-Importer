import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from model_routing import UsageLedger


_WORKER = """
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))
from model_routing import QuotaExhausted, UsageLedger
ledger = UsageLedger(Path(sys.argv[1]))
print('READY', flush=True)
sys.stdin.readline()
try:
    ledger.reserve('primary', 1)
    print('reserved', flush=True)
except QuotaExhausted:
    print('exhausted', flush=True)
"""


class ModelRoutingProcessTests(unittest.TestCase):
    def test_daily_limit_is_atomic_across_independent_processes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "usage.json"
            processes = [subprocess.Popen(
                [sys.executable, "-c", _WORKER, str(path)], cwd=Path.cwd(),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True,
            ) for _ in range(2)]
            try:
                ready = [process.stdout.readline().strip() for process in processes]
                self.assertEqual(ready, ["READY", "READY"])
                for process in processes:
                    process.stdin.write("go\n")
                    process.stdin.close()
                outputs = [process.communicate(timeout=15) for process in processes]
                self.assertEqual([process.returncode for process in processes], [0, 0],
                                 [error for _, error in outputs])
                outcomes = sorted(stdout.strip().splitlines()[-1] for stdout, _ in outputs)
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)
            requests = json.loads(path.read_text(encoding="utf-8"))["models"]["primary"]["requests"]

        self.assertEqual(outcomes, ["exhausted", "reserved"])
        self.assertEqual(requests, 1)


if __name__ == "__main__":
    unittest.main()
