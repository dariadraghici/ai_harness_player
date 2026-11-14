"""bot/resources.py: detection order, clamps, floors, idle zeroing, BOT_RESOURCES=off, failure paths.

Fake cgroup and /proc trees live in temp directories injected through the module-level roots."""

import os
import tempfile
import unittest

from bot import resources as R

MB = 1024 * 1024


def write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


class Base(unittest.TestCase):
    ENV = ("BOT_RESOURCES", "BOT_CPUS", "BOT_MEM_LIMIT_MB", "BOT_RESERVE_MB", "BOT_MAX_CHILDREN",
           "BOT_CHILD_MEM_MB", "BOT_SLICE_FRACTION", "BOT_SEARCH_WORKERS", "BOT_WARM_WORKER", "BOT_PREP_WORKERS")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cg = os.path.join(self.tmp.name, "cg")
        self.proc = os.path.join(self.tmp.name, "proc")
        os.makedirs(self.cg)
        os.makedirs(self.proc)
        self.saved = (R.CGROUP_ROOT, R.PROC_ROOT, {k: os.environ.pop(k, None) for k in self.ENV})
        R.CGROUP_ROOT, R.PROC_ROOT = self.cg, self.proc
        R.reset()
        self._aff = os.sched_getaffinity if hasattr(os, "sched_getaffinity") else None

    def tearDown(self):
        R.CGROUP_ROOT, R.PROC_ROOT = self.saved[0], self.saved[1]
        for k in self.ENV:
            os.environ.pop(k, None)
        for k, v in self.saved[2].items():
            if v is not None:
                os.environ[k] = v
        if self._aff is not None:
            os.sched_getaffinity = self._aff
        R.reset()
        self.tmp.cleanup()

    def affinity(self, n):
        os.sched_getaffinity = lambda pid: set(range(n))

    def container(self, cpus=2, mem_mb=2048, used_mb=100):
        self.affinity(cpus)
        write(self.cg, "memory.max", "%d\n" % (mem_mb * MB))
        write(self.cg, "memory.current", "%d\n" % (used_mb * MB))
        write(self.proc, "meminfo", "MemTotal:       %d kB\n" % (16384 * 1024))


class TestDetection(Base):
    def test_cpu_min_of_affinity_and_quota_v2(self):
        self.affinity(8)
        write(self.cg, "cpu.max", "200000 100000\n")
        self.assertEqual(R.cpus(), 2)

    def test_cpu_quota_max_means_unlimited(self):
        self.affinity(3)
        write(self.cg, "cpu.max", "max 100000\n")
        self.assertEqual(R.cpus(), 3)

    def test_cpu_quota_rounds_up_and_floor_is_one(self):
        self.affinity(8)
        write(self.cg, "cpu.max", "150000 100000\n")
        self.assertEqual(R.cpus(), 2)
        R.reset()
        write(self.cg, "cpu.max", "10000 100000\n")
        self.assertEqual(R.cpus(), 1)

    def test_cpu_cgroup_v1(self):
        self.affinity(8)
        write(self.cg, "cpu/cpu.cfs_quota_us", "300000\n")
        write(self.cg, "cpu/cpu.cfs_period_us", "100000\n")
        self.assertEqual(R.cpus(), 3)

    def test_cpu_v1_unlimited_quota(self):
        self.affinity(4)
        write(self.cg, "cpu/cpu.cfs_quota_us", "-1\n")
        write(self.cg, "cpu/cpu.cfs_period_us", "100000\n")
        self.assertEqual(R.cpus(), 4)

    def test_cpu_fallback_chain(self):
        def boom(pid):
            raise OSError("no affinity")
        os.sched_getaffinity = boom
        self.assertEqual(R.cpus(), max(1, os.cpu_count() or 2))
        R.reset()
        saved = os.cpu_count
        os.cpu_count = lambda: None
        try:
            self.assertEqual(R.cpus(), 2)
        finally:
            os.cpu_count = saved

    def test_memory_limit_order_v2_then_v1_then_meminfo_then_default(self):
        write(self.proc, "meminfo", "MemTotal:       8388608 kB\n")
        write(self.cg, "memory.max", "%d\n" % (2048 * MB))
        write(self.cg, "memory/memory.limit_in_bytes", "%d\n" % (1024 * MB))
        self.assertEqual(R.mem_limit_mb(), 2048)
        os.remove(os.path.join(self.cg, "memory.max"))
        R.reset()
        self.assertEqual(R.mem_limit_mb(), 1024)
        os.remove(os.path.join(self.cg, "memory", "memory.limit_in_bytes"))
        R.reset()
        self.assertEqual(R.mem_limit_mb(), 8192)
        os.remove(os.path.join(self.proc, "meminfo"))
        R.reset()
        self.assertEqual(R.mem_limit_mb(), 2048)

    def test_memory_max_word_max_and_absurd_values_fall_back_to_memtotal(self):
        write(self.proc, "meminfo", "MemTotal:       4194304 kB\n")
        write(self.cg, "memory.max", "max\n")
        self.assertEqual(R.mem_limit_mb(), 4096)
        R.reset()
        write(self.cg, "memory.max", "9223372036854771712\n")       # the v1/v2 "no limit" sentinel
        self.assertEqual(R.mem_limit_mb(), 4096)

    def test_garbage_files_never_raise(self):
        write(self.cg, "cpu.max", "not a number\n")
        write(self.cg, "memory.max", "???\n")
        write(self.cg, "memory.current", "\x00\x01\n")
        write(self.proc, "meminfo", "MemTotal: lots kB\n")
        self.assertGreaterEqual(R.cpus(), 1)
        self.assertEqual(R.mem_limit_mb(), 2048)
        self.assertGreaterEqual(R.mem_used_mb(), 0)
        self.assertIn("cpus", R.describe())

    def test_used_memory_ignores_reclaimable_page_cache(self):
        write(self.cg, "memory.current", "%d\n" % (900 * MB))
        write(self.cg, "memory.stat", "anon 1000\ninactive_file %d\nactive_file 5\n" % (600 * MB))
        self.assertEqual(R.mem_used_mb(), 300)

    def test_used_memory_falls_back_to_proc_status(self):
        write(self.proc, "self/status", "Name: x\nVmRSS:\t   204800 kB\n")
        self.assertGreaterEqual(R.mem_used_mb(), 200)

    def test_probes_are_cached_but_used_memory_is_live(self):
        self.container(cpus=2, mem_mb=2048, used_mb=100)
        self.assertEqual(R.mem_limit_mb(), 2048)
        write(self.cg, "memory.max", "%d\n" % (512 * MB))
        self.assertEqual(R.mem_limit_mb(), 2048)                 # cached after the first probe
        write(self.cg, "memory.current", "%d\n" % (300 * MB))
        self.assertEqual(R.mem_used_mb(), 300)

    def test_explicit_knobs_win_over_detection(self):
        self.container(cpus=2, mem_mb=2048)
        os.environ["BOT_CPUS"] = "3"
        os.environ["BOT_MEM_LIMIT_MB"] = "4096"
        self.assertEqual(R.cpus(), 3)
        self.assertEqual(R.mem_limit_mb(), 4096)
        os.environ["BOT_CPUS"] = "banana"
        self.assertEqual(R.cpus(), 2)                            # not a number: detection


class TestReserve(Base):
    def test_reserve_is_max_of_256_and_20_percent(self):
        self.container(mem_mb=2048)
        self.assertEqual(R.reserve_mb(), 409)
        R.reset()
        write(self.cg, "memory.max", "%d\n" % (1024 * MB))
        self.assertEqual(R.reserve_mb(), 256)

    def test_available_never_negative(self):
        self.container(mem_mb=1024, used_mb=1000)
        self.assertEqual(R.mem_available_mb(), 0)


class TestPlans(Base):
    def test_contract_container_never_below_today(self):
        self.container(cpus=2, mem_mb=2048, used_mb=100)
        p = R.plan("turn_search")
        self.assertEqual(p.children, 2)
        self.assertGreaterEqual(p.child_mem_mb, R.TODAY_CHILD_MEM_MB)
        self.assertGreaterEqual(p.tt_entries, R.TODAY_TT_ENTRIES)
        self.assertIsNone(p.slice_fraction)             # the engine keeps its own default
        self.assertEqual(p.search_workers, 2)
        self.assertTrue(p.warm_worker)

    def test_children_times_memory_fits_available_memory(self):
        for cpus, mem, used in ((2, 2048, 100), (2, 2048, 900), (4, 4096, 200), (16, 8192, 50), (1, 1024, 100)):
            R.reset()
            self.container(cpus, mem, used)
            for ph in ("prep", "turn"):
                R.set_phase(ph)
                for kind in R.KINDS:
                    p = R.plan(kind)
                    self.assertGreaterEqual(p.children, 1)
                    self.assertLessEqual(p.children * p.child_mem_mb, max(R.mem_available_mb(), R.MIN_CHILD_MEM_MB),
                                         (cpus, mem, used, ph, kind, p))
                    self.assertGreaterEqual(p.child_mem_mb, R.MIN_CHILD_MEM_MB)
                    self.assertLessEqual(p.child_mem_mb, R.MAX_CHILD_MEM_MB)

    def test_cpu_bound_children_never_exceed_cpus_and_nothing_exceeds_cpus_plus_one(self):
        self.container(cpus=3, mem_mb=8192, used_mb=100)
        for kind in R.CPU_BOUND:
            self.assertLessEqual(R.plan(kind).children, 3)
        self.assertLessEqual(R.plan("prep_codegen").children, 4)
        os.environ["BOT_MAX_CHILDREN"] = "50"
        self.assertLessEqual(R.plan("prep_verify").children, 3)

    def test_one_cpu_is_below_today_and_that_is_allowed(self):
        self.container(cpus=1, mem_mb=2048)
        p = R.plan("prep_verify")
        self.assertEqual(p.children, 1)
        self.assertEqual(p.search_workers, 1)

    def test_low_memory_shrinks_the_number_of_children_before_the_memory_of_each(self):
        self.container(cpus=8, mem_mb=2048, used_mb=900)         # available: 2048 - 900 - 409 = 739
        p = R.plan("prep_verify")
        self.assertEqual(p.children, 1)
        self.assertGreaterEqual(p.child_mem_mb, R.TODAY_CHILD_MEM_MB)

    def test_child_memory_has_a_floor_when_nothing_is_available(self):
        self.container(cpus=2, mem_mb=1024, used_mb=1000)
        p = R.plan("turn_search")
        self.assertEqual((p.children, p.child_mem_mb), (1, R.MIN_CHILD_MEM_MB))

    def test_idle_phase_is_zero_children_and_nothing_warm(self):
        self.container()
        R.set_phase("idle")
        for kind in R.KINDS:
            p = R.plan(kind)
            self.assertEqual(p.children, 0)
            self.assertFalse(p.warm_worker)
            self.assertEqual(p.phase, "idle")

    def test_set_phase_ignores_garbage(self):
        R.set_phase("prep")
        R.set_phase("nonsense")
        self.assertEqual(R.phase(), "prep")
        R.set_phase(None)
        self.assertEqual(R.phase(), "prep")

    def test_default_phase_is_turn(self):
        self.assertEqual(R.phase(), "turn")

    def test_off_switch_returns_exactly_todays_constants(self):
        self.container(cpus=16, mem_mb=16384)
        os.environ["BOT_RESOURCES"] = "off"
        os.environ["BOT_CPUS"] = "8"
        os.environ["BOT_SEARCH_WORKERS"] = "4"
        for ph in ("prep", "turn"):
            R.set_phase(ph)
            for kind in R.KINDS:
                p = R.plan(kind)
                self.assertEqual((p.children, p.child_mem_mb, p.child_cpu_s, p.slice_fraction, p.tt_entries,
                                  p.cache_roots, p.search_workers, p.samples_scale, p.warm_worker),
                                 (2, 512, 120, None, 350000, 200, 1, 1.0, False))

    def test_explicit_knobs_are_clamped_to_what_is_safe(self):
        self.container(cpus=2, mem_mb=2048, used_mb=100)
        os.environ["BOT_CHILD_MEM_MB"] = "4096"
        p = R.plan("turn_search")
        self.assertLessEqual(p.children * p.child_mem_mb, R.mem_available_mb())
        os.environ["BOT_CHILD_MEM_MB"] = "10"
        self.assertEqual(R.plan("turn_search").child_mem_mb, R.MIN_CHILD_MEM_MB)
        os.environ["BOT_SEARCH_WORKERS"] = "1"
        self.assertEqual(R.plan("turn_search").search_workers, 1)
        os.environ["BOT_SEARCH_WORKERS"] = "9"
        self.assertLessEqual(R.plan("turn_search").search_workers, R.plan("turn_search").children)
        os.environ["BOT_SLICE_FRACTION"] = "0.9"
        self.assertEqual(R.plan("turn_search").slice_fraction, 0.40)
        os.environ["BOT_WARM_WORKER"] = "off"
        self.assertFalse(R.plan("turn_search").warm_worker)

    def test_plan_never_raises_even_when_the_probes_do(self):
        os.sched_getaffinity = None                              # calling None raises inside the probe
        write(self.cg, "memory.max", "\xff\xfe")
        self.assertIsInstance(R.plan("turn_search"), R.Plan)
        self.assertIsInstance(R.plan(None), R.Plan)
        self.assertIsInstance(R.describe(), dict)

    def test_prep_phase_gets_a_bigger_cache_and_more_threads(self):
        self.container(cpus=2, mem_mb=2048)
        R.set_phase("prep")
        p = R.plan("prep_solve")
        self.assertGreater(p.cache_roots, R.TODAY_CACHE_ROOTS)
        R.set_phase("turn")
        self.assertEqual(R.plan("turn_search").cache_roots, p.cache_roots)      # the match must not truncate prep's roots
        R.set_phase("prep")
        self.assertGreaterEqual(p.threads, R.TODAY_THREADS)
        self.assertFalse(p.warm_worker)

    def test_describe_is_one_flat_json_able_dict(self):
        import json
        self.container()
        d = R.describe()
        json.dumps(d)
        for k in ("cpus", "mem_limit_mb", "mem_available_mb", "reserve_mb", "children", "phase"):
            self.assertIn(k, d)


class TodayConstantsMatchTheCode(unittest.TestCase):
    """BOT_RESOURCES=off promises today's behavior: the floor numbers in resources.py must be the numbers in the code."""

    def test_floor_equals_the_constants_it_mirrors(self):
        from bot.engines import base, search
        from bot.prep import sandbox
        self.assertEqual(R.TODAY_CHILDREN, sandbox.MAX_CHILDREN)
        self.assertEqual(R.TODAY_CHILD_MEM_MB, sandbox.CHILD_MEMORY_MB)
        self.assertEqual(R.TODAY_CHILD_CPU_S, sandbox.CHILD_CPU_S)
        self.assertEqual(R.TODAY_SLICE_FRACTION, base.SLICE_FRACTION)
        self.assertEqual(R.TODAY_CACHE_ROOTS, search.CACHE_MAX_ROOTS)

    def test_job_defaults_are_the_old_caps_when_the_payload_has_no_field(self):
        import importlib.util
        import os
        root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot", "engines")
        spec = importlib.util.spec_from_file_location("search_job_for_test", os.path.join(root, "search_job.py"))
        job = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(job)
        self.assertEqual(R.TODAY_TT_ENTRIES, job.TT_CAP)
        self.assertEqual(job.AlphaBeta(None, None, None, 0).cap, job.TT_CAP)
        self.assertEqual(job.AlphaBeta(None, None, None, 0, 5000).cap, 5000)
        self.assertEqual(job._part({}), (0, 1))
        self.assertEqual(job._part({"part": 1, "parts": 2}), (1, 2))
        self.assertEqual(job._part({"part": 5, "parts": 2}), (0, 1))
        self.assertEqual(job._part({"part": "x", "parts": 2}), (0, 1))


if __name__ == "__main__":
    unittest.main()
