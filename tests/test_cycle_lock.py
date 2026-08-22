"""Unit tests for scripts/cycle_lock.py's explicit acquire()/release() API --
separate from TestCycleLock in test_paper_trading.py, which covers the `with`
(context-manager) interface. The explicit API matters on its own: the live
trade-cycle skill isn't a single Python process that can hold a `with` block
open across a whole cycle (its "body" is many separate tool calls issued by
an LLM session, not one contiguous script run) -- it has to call acquire()
in one process/invocation and release() in a separate, later one, which is
exactly what this file exercises.
Run: python3 -m unittest discover -s tests -v"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.cycle_lock import CycleLock  # noqa: E402


class TestCycleLockExplicitAcquireRelease(unittest.TestCase):
    def setUp(self):
        self.lock_path = Path(__file__).resolve().parent / "_tmp_cycle_lock_explicit_test.lock"
        if self.lock_path.exists():
            self.lock_path.unlink()

    def tearDown(self):
        if self.lock_path.exists():
            self.lock_path.unlink()

    def test_acquire_creates_the_lock_file(self):
        lock = CycleLock(self.lock_path)
        lock.acquire()
        try:
            self.assertTrue(self.lock_path.exists())
        finally:
            lock.release()

    def test_release_removes_the_lock_file(self):
        lock = CycleLock(self.lock_path)
        lock.acquire()
        lock.release()
        self.assertFalse(self.lock_path.exists())

    def test_release_is_safe_to_call_when_nothing_is_held(self):
        # mirrors a live cycle that errors before ever acquiring, or double-releases
        lock = CycleLock(self.lock_path)
        lock.release()  # must not raise
        self.assertFalse(self.lock_path.exists())

    def test_acquire_in_one_instance_blocks_a_second_instance(self):
        # models two separate CLI invocations (two separate CycleLock objects,
        # as the live skill would create) rather than one shared Python object
        first = CycleLock(self.lock_path, timeout=5.0, poll=0.02)
        first.acquire()
        second = CycleLock(self.lock_path, timeout=0.3, poll=0.02)
        # second.acquire() would block for up to its timeout then break the
        # "stale" lock -- prove it does NOT return immediately while first
        # still legitimately holds it
        import time
        start = time.time()
        second.acquire()  # will break the lock as "stale" once its own timeout elapses
        elapsed = time.time() - start
        self.assertGreaterEqual(elapsed, 0.3, "a second acquire must not succeed while the first still holds the lock")
        second.release()
        first.release()  # already gone (broken as stale), must not raise

    def test_stale_lock_from_a_crashed_process_is_broken(self):
        # a lock file with no CycleLock instance behind it at all -- e.g. a
        # live cycle that crashed after acquire() but before release()
        self.lock_path.write_text("")
        lock = CycleLock(self.lock_path, timeout=0.2, poll=0.05)
        import time
        start = time.time()
        lock.acquire()
        self.assertLess(time.time() - start, 5.0, "a stale lock should be broken quickly, not hang forever")
        lock.release()


if __name__ == "__main__":
    unittest.main()
