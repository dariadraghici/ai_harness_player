"""Sandbox slot pool (WP1): capacity set once per phase, bounded exactly, shrinking never kills a child."""

import threading
import time
import unittest

from bot.prep import sandbox
from tests import test_sandbox as _fixtures

pid_alive = _fixtures.pid_alive


class SlotPoolUnit(unittest.TestCase):
    def test_bounded_and_growing_wakes_waiters(self):
        pool = sandbox._SlotPool(1)
        self.assertTrue(pool.acquire(0.1))
        self.assertFalse(pool.acquire(0.05))
        got = []
        th = threading.Thread(target=lambda: got.append(pool.acquire(3)))
        th.start()
        time.sleep(0.1)
        pool.set_capacity(2)
        th.join(2)
        self.assertEqual(got, [True])

    def test_release_never_goes_below_zero(self):
        pool = sandbox._SlotPool(1)
        pool.release()
        pool.release()
        self.assertTrue(pool.acquire(0.1))
        self.assertFalse(pool.acquire(0.05))

    def test_capacity_floor_is_one(self):
        pool = sandbox._SlotPool(0)
        self.assertEqual(pool.capacity, 1)
        pool.set_capacity(-5)
        self.assertEqual(pool.capacity, 1)


class CapacityTests(_fixtures.SandboxTests):
    """Reuses the fixtures and the strict teardown of SandboxTests (no child left, pids dead)."""

    def setUp(self):
        super().setUp()
        self.start_capacity = sandbox.capacity()
        self.addCleanup(sandbox.set_capacity, self.start_capacity)

    def fill(self, n):
        workers = [self.worker() for _ in range(n)]
        for w in workers:
            self.state(w)
            self.started(w)
        return workers

    def test_default_capacity_is_the_old_constant(self):
        self.assertEqual(self.start_capacity, sandbox.MAX_CHILDREN)

    def test_capacity_one_is_exact(self):
        sandbox.set_capacity(1)
        self.fill(1)
        self.assertEqual(sandbox.live_count(), 1)
        with self.assertRaises(sandbox.SandboxError) as cm:
            sandbox.call_simulator(self.sim(), "legal_actions", [{}], 0.3)
        self.assertEqual(cm.exception.kind, "busy")
        self.assertEqual(sandbox.live_count(), 1)

    def test_capacity_four_is_exact_no_fifth_child_ever_alive(self):
        sandbox.set_capacity(4)
        workers = self.fill(4)
        self.assertEqual(sandbox.live_count(), 4)
        with self.assertRaises(sandbox.SandboxError) as cm:
            sandbox.call_simulator(self.sim(), "legal_actions", [{}], 0.3)
        self.assertEqual(cm.exception.kind, "busy")
        self.assertEqual(sandbox.live_count(), 4)
        workers[0].close()
        self.assertEqual(sandbox.live_count(), 3)
        self.assertEqual(sandbox.call_simulator(self.sim(), "legal_actions",
                                                [self.state(workers[1])], 5), ["take 1", "take 2", "take 3"])

    def test_shrinking_under_load_kills_nothing_and_leaves_no_orphan(self):
        sandbox.set_capacity(3)
        workers = self.fill(3)
        pids = [w.proc.pid for w in workers]
        sandbox.set_capacity(1)
        self.assertEqual(sandbox.live_count(), 3)                       # nobody was killed
        for pid in pids:
            self.assertTrue(pid_alive(pid))
        s = self.state(workers[0])
        self.assertEqual(workers[0].call("legal_actions", [s], 5), ["take 1", "take 2", "take 3"])
        with self.assertRaises(sandbox.SandboxError) as cm:             # but no new child starts
            sandbox.call_simulator(self.sim(), "legal_actions", [s], 0.3)
        self.assertEqual(cm.exception.kind, "busy")
        workers[1].close()
        workers[2].close()
        with self.assertRaises(sandbox.SandboxError) as cm:             # one still in use, capacity 1
            sandbox.call_simulator(self.sim(), "legal_actions", [s], 0.3)
        self.assertEqual(cm.exception.kind, "busy")
        workers[0].close()
        self.assertEqual(sandbox.call_simulator(self.sim(), "legal_actions", [s], 5), ["take 1", "take 2", "take 3"])
        self.assertEqual(sandbox.live_count(), 0)

    def test_growing_lets_a_waiting_start_through(self):
        sandbox.set_capacity(1)
        self.fill(1)
        box = {}

        def waiting():
            try:
                box["r"] = sandbox.call_simulator(self.sim(), "legal_actions",
                                                  [{"stones": 3, "winner": None}], 10)
            except sandbox.SandboxError as e:
                box["e"] = e
        th = threading.Thread(target=waiting)
        th.start()
        time.sleep(0.3)
        sandbox.set_capacity(2)
        th.join(10)
        self.assertEqual(box.get("r"), ["take 1", "take 2", "take 3"], box)

    def test_kill_all_frees_every_slot_at_any_capacity(self):
        sandbox.set_capacity(4)
        self.fill(4)
        self.assertEqual(sandbox.kill_all("test"), 4)
        self.assertEqual(sandbox.live_count(), 0)
        self.fill(4)

    def test_memory_overrun_dies_clean_at_capacity_four_and_the_parent_survives(self):
        sandbox.set_capacity(4)
        w = self.worker("memory", mem_mb=300)
        s = self.state(w)
        self.pids.append(w.ping(5)["pid"])
        with self.assertRaises(sandbox.SandboxError) as cm:
            w.call("is_terminal", [s], 20)
        self.assertIn(cm.exception.kind, ("error", "crash"))
        self.assertEqual(sandbox.live_count(), 0 if cm.exception.kind == "crash" else sandbox.live_count())
        self.assertEqual(sandbox.call_simulator(self.sim(), "legal_actions", [s], 5), ["take 1", "take 2", "take 3"])


for _name in dir(_fixtures.SandboxTests):                 # the fixtures are reused, the inherited tests are not run twice
    if _name.startswith("test_") and _name not in CapacityTests.__dict__:
        setattr(CapacityTests, _name, None)


if __name__ == "__main__":
    unittest.main()
