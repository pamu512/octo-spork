"""Tests for :mod:`agent_guard.session_store`."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class SessionStoreTests(unittest.TestCase):
    def test_push_and_get_roundtrip(self) -> None:
        from agent_guard.session_store import SessionStore

        fake = MagicMock()
        store_data: dict[str, str] = {}

        def fake_get(key: str) -> str | None:
            return store_data.get(key)

        def fake_set(key: str, val: str, *a: object, **kw: object) -> bool:
            store_data[key] = val
            return True

        def fake_expire(*_a: object, **_k: object) -> bool:
            return True

        fake.get.side_effect = fake_get
        fake.set.side_effect = fake_set
        fake.expire.side_effect = fake_expire

        with patch("redis.Redis.from_url", return_value=fake):
            s = SessionStore(redis_url="redis://localhost:9999/0", key_prefix="test:lg")
            s.push_snapshot("thread-abc", {"messages": [{"role": "user", "content": "hi"}]})
            raw = store_data.get("test:lg:latest")
            self.assertIsNotNone(raw)
            obj = json.loads(str(raw))
            self.assertEqual(obj["thread_id"], "thread-abc")
            self.assertEqual(obj["values"]["messages"][0]["content"], "hi")

            snap = s.get_latest_snapshot()
            self.assertIsNotNone(snap)
            self.assertEqual(snap["thread_id"], "thread-abc")

    def test_periodic_save_invokes_push(self) -> None:
        from agent_guard.session_store import SessionStore

        pushes: list[tuple[str, dict[str, str]]] = []

        def capture_push(self, tid: str, vals: dict[str, str]) -> None:
            pushes.append((tid, vals))

        calls = {"n": 0}

        def supplier() -> tuple[str, dict[str, str]] | None:
            calls["n"] += 1
            if calls["n"] > 2:
                return None
            return ("t1", {"x": str(calls["n"])})

        fake = MagicMock()
        store_data: dict[str, str] = {}

        def fake_set(key: str, val: str, *a: object, **kw: object) -> bool:
            store_data[key] = val
            return True

        fake.get.return_value = None
        fake.set.side_effect = fake_set
        fake.expire.return_value = True

        with patch("redis.Redis.from_url", return_value=fake):
            s = SessionStore(redis_url="redis://x", key_prefix="p")
            with patch.object(SessionStore, "push_snapshot", capture_push):
                h = s.start_periodic_save(0.05, supplier)
                threading.Event().wait(0.25)
                h.stop(join_timeout=2.0)

        self.assertGreaterEqual(len(pushes), 1)


class ResumeCommandTests(unittest.TestCase):
    def test_resume_writes_json(self) -> None:
        import local_ai_stack.__main__ as main_mod

        snap = {
            "version": 1,
            "thread_id": "tid-1",
            "values": {"foo": 1},
            "updated_at": "2026-01-01T00:00:00+00:00",
        }

        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            env_path = ws / ".env.local"
            env_path.write_text("REDIS_URL=redis://127.0.0.1:9/0\n", encoding="utf-8")

            fake_store = MagicMock()
            fake_store.get_latest_snapshot.return_value = snap

            with patch("agent_guard.session_store.SessionStore", return_value=fake_store):
                rc = main_mod.command_resume(
                    env_path,
                    ws,
                    claude=False,
                    agent_cmd=[],
                    print_json=False,
                )

            self.assertEqual(rc, 0)
            out = ws / ".local" / "octo_session" / "langgraph_resume.json"
            self.assertTrue(out.is_file())
            data = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(data["thread_id"], "tid-1")


if __name__ == "__main__":
    unittest.main()
