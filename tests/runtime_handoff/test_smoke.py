from datetime import UTC, datetime
from pathlib import Path
import tempfile
import unittest

from code_mule.runtime_handoff import InvalidDeliveryManifest, parse_manifest_candidate
from code_mule.runtime_handoff.service import RuntimeSmokeVerifier

from runtime_handoff.test_contracts import candidate


NOW = datetime(2026, 9, 14, tzinfo=UTC)


class FakeProcess:
    pid = 41000

    def __init__(self):
        self.running = True
        self.terminated = 0
        self.killed = 0

    def poll(self):
        return None if self.running else 0

    def terminate(self):
        self.terminated += 1
        self.running = False

    def kill(self):
        self.killed += 1
        self.running = False

    def wait(self, timeout=None):
        if self.running:
            raise __import__("subprocess").TimeoutExpired("runtime", timeout)
        return 0


class RuntimeSmokeVerifierTests(unittest.TestCase):
    def manifest(self, root: Path):
        (root / "index.html").write_text("ok", encoding="utf-8")
        payload = candidate()
        payload["runtime_generated_paths"] = []
        return parse_manifest_candidate(
            payload,
            project_id="project-1",
            revision_number=1,
            plan_version=1,
            generated_at=NOW,
        )

    def test_structured_launch_local_health_and_bounded_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self.manifest(root)
            process = FakeProcess()
            launches = []
            urls = []

            def popen(argv, **kwargs):
                launches.append((tuple(argv), kwargs))
                return process

            verifier = RuntimeSmokeVerifier(
                environment={"PATH": "/usr/bin:/bin", "API_KEY": "secret"},
                popen=popen,
                http_status=lambda url, timeout: urls.append(url) or 200,
                dynamic_port_factory=lambda: 43123,
                port_available=lambda port: True,
                sleeper=lambda seconds: None,
            )
            verifier.verify(manifest, root)

            self.assertEqual(Path(launches[0][0][0]).name, "python3")
            self.assertEqual(
                launches[0][0][1:],
                ("-m", "http.server", "43123", "--bind", "127.0.0.1"),
            )
            self.assertFalse(launches[0][1]["shell"])
            self.assertNotIn("API_KEY", launches[0][1]["env"])
            self.assertEqual(urls, ["http://127.0.0.1:43123/"])
            self.assertEqual((process.terminated, process.killed), (1, 0))

    def test_failed_health_still_stops_owned_child(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = self.manifest(root)
            process = FakeProcess()
            ticks = iter((0.0, 4.0, 4.0))
            verifier = RuntimeSmokeVerifier(
                environment={"PATH": "/usr/bin:/bin"},
                popen=lambda *args, **kwargs: process,
                http_status=lambda url, timeout: None,
                dynamic_port_factory=lambda: 43124,
                sleeper=lambda seconds: None,
                monotonic=lambda: next(ticks),
            )
            with self.assertRaisesRegex(InvalidDeliveryManifest, "health check"):
                verifier.verify(manifest, root)
            self.assertEqual((process.terminated, process.killed), (1, 0))


if __name__ == "__main__":
    unittest.main()
