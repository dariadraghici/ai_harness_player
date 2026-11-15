"""/memory is shared by every container of ours AT THE SAME TIME (ARENA-CONTRACT 2.1, rulebook 5.14).

A dozen specialists of ours are alive during a tournament, they all write into the same directory, and a
half-written file would be read by the next container as the truth. These tests start real subprocesses (one
per "container") against one directory and check what the harness promises:

  * a file is never seen half written: every reader sees the old bytes or the new bytes (atomic rename);
  * a shared read-modify-write file (the solved-roots cache) may lose an update under a race, which is
    documented and harmless, but may never end up corrupt;
  * a variant written by another harness version is ignored, not loaded (cache versioning);
  * the install lock of the engine subsystem is really exclusive between processes;
  * the temporary files of a killed container do not pile up or get read.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

WRITER = r'''
import json, os, sys, time
sys.path.insert(0, %(repo)r)
from bot import memory
memory.reset_root_for_tests(%(root)r)
who, rounds = int(sys.argv[1]), int(sys.argv[2])
payload = {"who": who, "blob": "x" * 20000}
for i in range(rounds):
    memory.write_json_atomic(os.path.join(memory.root(), "shared.json"), dict(payload, i=i))
    memory.write_json_atomic(os.path.join(memory.root(), "own-%%d.json" %% who), dict(payload, i=i))
    memory.append_jsonl(os.path.join(memory.root(), "metrics", "c%%d.jsonl" %% who), {"who": who, "i": i})
print("done", who)
'''

READER = r'''
import json, os, sys, time
sys.path.insert(0, %(repo)r)
from bot import memory
memory.reset_root_for_tests(%(root)r)
path = os.path.join(memory.root(), "shared.json")
bad, reads = 0, 0
end = time.time() + float(sys.argv[1])
while time.time() < end:
    try:
        with open(path, "r") as f:
            data = json.load(f)
        reads += 1
        if not isinstance(data, dict) or "who" not in data or len(data.get("blob") or "") != 20000:
            bad += 1
    except FileNotFoundError:
        pass
    except ValueError:
        bad += 1          # a half-written file: this must never happen
print(json.dumps({"reads": reads, "bad": bad}))
'''

ROOTS = r'''
import os, sys
sys.path.insert(0, %(repo)r)
from bot import memory
memory.reset_root_for_tests(%(root)r)
from bot.engines import search
from bot.interfaces import GameProfile, VariantSpec
spec = VariantSpec(game_id="g", game_name="g", label="l", rules="r", rules_hash="h", profile=GameProfile())
memory.write_json_atomic(os.path.join(memory.variant_dir("g", "h"), "verify.json"),
                         {"winner": {"sha256": "SHA"}})
who, rounds = int(sys.argv[1]), int(sys.argv[2])
for i in range(rounds):
    search.save_root(spec, "root-%%d-%%d" %% (who, i), {"a": 1.0})
print("done")
'''


def run(script, root, args, timeout=120):
    code = script % {"repo": REPO, "root": root}
    return subprocess.Popen([sys.executable, "-c", code] + [str(a) for a in args],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


class ConcurrencyTests(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="ld-shared-")

    def test_a_reader_never_sees_a_half_written_file(self):
        writers = [run(WRITER, self.root, [i, 25]) for i in range(5)]
        readers = [run(READER, self.root, [4.0]) for _ in range(3)]
        for p in writers:
            out, err = p.communicate(timeout=120)
            self.assertEqual(p.returncode, 0, err[-400:])
        total, bad = 0, 0
        for p in readers:
            out, err = p.communicate(timeout=120)
            self.assertEqual(p.returncode, 0, err[-400:])
            row = json.loads(out.strip().splitlines()[-1])
            total += row["reads"]
            bad += row["bad"]
        self.assertGreater(total, 10, "the readers did not get to read anything")
        self.assertEqual(bad, 0, "a reader saw a file that was not complete")

    def test_no_temporary_file_is_left_behind(self):
        for p in [run(WRITER, self.root, [i, 10]) for i in range(4)]:
            p.communicate(timeout=120)
        leftovers = [n for n in os.listdir(self.root) if n.endswith(".tmp") or ".tmp" in n]
        self.assertEqual(leftovers, [])

    def test_every_writer_keeps_its_own_file_and_its_own_metrics(self):
        for p in [run(WRITER, self.root, [i, 15]) for i in range(4)]:
            p.communicate(timeout=120)
        for who in range(4):
            with open(os.path.join(self.root, "own-%d.json" % who)) as f:
                self.assertEqual(json.load(f)["who"], who)
            with open(os.path.join(self.root, "metrics", "c%d.jsonl" % who)) as f:
                rows = [json.loads(line) for line in f if line.strip()]
            self.assertEqual(len(rows), 15)
            self.assertEqual(sorted(r["i"] for r in rows), list(range(15)))

    def test_a_shared_read_modify_write_cache_may_lose_updates_but_never_breaks(self):
        """engines/search.save_root is a read-modify-write on one file in /memory: several containers racing
        on it may lose an entry (that only costs a cache hit), but the file must always parse."""
        for p in [run(ROOTS, self.root, [i, 12]) for i in range(4)]:
            out, err = p.communicate(timeout=180)
            self.assertEqual(p.returncode, 0, err[-400:])
        path = os.path.join(self.root, "g", "h", "solved.json")
        with open(path) as f:
            data = json.load(f)
        self.assertEqual(data["sim"], "SHA")
        keys = [r[0] for r in data["roots"]]
        self.assertEqual(len(keys), len(set(keys)), "the same root was stored twice")
        self.assertTrue(keys, "every update was lost")
        self.assertLessEqual(len(keys), 48)

    def test_a_variant_of_another_harness_version_is_not_loaded(self):
        sys.path.insert(0, REPO)
        from bot import config, memory
        from bot.interfaces import GameProfile, VariantSpec
        memory.reset_root_for_tests(self.root)
        self.addCleanup(memory.reset_root_for_tests, None)
        spec = VariantSpec(game_id="g2", game_name="g2", label="l", rules="rules text",
                           rules_hash=memory.rules_hash("rules text"), profile=GameProfile(family="think_ahead"))
        memory.save_variant(spec)
        self.assertIsNotNone(memory.load_variant("g2", spec.rules_hash, "rules text"))
        meta_path = os.path.join(memory.variant_dir("g2", spec.rules_hash), "meta.json")
        with open(meta_path) as f:
            meta = json.load(f)
        self.assertEqual(meta["harness_version"], config.HARNESS_VERSION)
        meta["harness_version"] = "0.0.1-from-another-image"
        memory.write_json_atomic(meta_path, meta)
        self.assertIsNone(memory.load_variant("g2", spec.rules_hash, "rules text"))

    def test_the_install_lock_is_exclusive_between_processes(self):
        script = r'''
import os, sys, time
sys.path.insert(0, %(repo)r)
from bot import memory
memory.reset_root_for_tests(%(root)r)
from bot.libs import install
path = os.path.join(memory.root(), ".lock-demo")
with install._Lock(path, timeout=float(sys.argv[2])):
    print("held", sys.argv[1], time.time())
    time.sleep(float(sys.argv[3]))
print("released", sys.argv[1], time.time())
'''
        first = run(script, self.root, ["a", 10, 1.5])
        time.sleep(0.4)
        second = run(script, self.root, ["b", 10, 0.1])
        out_a, err_a = first.communicate(timeout=60)
        out_b, err_b = second.communicate(timeout=60)
        self.assertEqual((first.returncode, second.returncode), (0, 0), err_a[-300:] + err_b[-300:])
        held_b = float([l for l in out_b.splitlines() if l.startswith("held")][0].split()[-1])
        released_a = float([l for l in out_a.splitlines() if l.startswith("released")][0].split()[-1])
        self.assertGreaterEqual(held_b, released_a - 0.05, "the second process entered while the first held it")


if __name__ == "__main__":
    unittest.main()
