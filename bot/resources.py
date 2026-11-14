"""One source of truth for CPU and memory: what the container has, what each phase may take.

Owner: luckydevs-1 (WP0 of PROMPT-RESOURCES.md). Status: real. Standard library only.

Every probe is best effort and cached after the first call; nothing in this module raises. The
numbers are MEASURED at run time (affinity mask, cgroup v2, cgroup v1, /proc), never taken from the
2 CPU / 2 GB of the arena contract. Callers ask `plan(kind)` and use the fields of the Plan; they
never compute a limit of their own.

Three phases (set_phase): "prep" (one prep container, nothing else in flight), "turn" (one move; the
default of a process that never calls set_phase, which is what tools and tests are), "idle" (between
games: every plan is zero children and nothing is kept warm).

Safety rules the planner obeys:
  * children * child_mem_mb <= mem_available_mb(), where available = limit - used - reserve and the
    reserve is max(256 MB, 20 percent of the limit): a child that wants more dies of its own
    RLIMIT_AS (a clean MemoryError), never of the cgroup OOM killer;
  * CPU-bound children never exceed cpus(); nothing exceeds cpus() + 1;
  * today's hard-coded numbers are the FLOOR (2 children, 512 MB, slice 0.15, tt 350000), unless the
    detected limits are really smaller;
  * BOT_RESOURCES=off returns exactly today's constants. Other knobs (all BOT_*): BOT_CPUS,
    BOT_MEM_LIMIT_MB, BOT_RESERVE_MB, BOT_MAX_CHILDREN, BOT_CHILD_MEM_MB, BOT_SLICE_FRACTION,
    BOT_SEARCH_WORKERS, BOT_WARM_WORKER, BOT_PREP_WORKERS. An explicit value wins over detection
    and is clamped to what is safe.
"""

import os
import threading
from collections import namedtuple

# ---------------------------------------------------------------- today's constants (the floor, and BOT_RESOURCES=off)
TODAY_CHILDREN = 2               # bot/prep/sandbox.py MAX_CHILDREN
TODAY_CHILD_MEM_MB = 512         # bot/prep/sandbox.py CHILD_MEMORY_MB
TODAY_CHILD_CPU_S = 120          # bot/prep/sandbox.py CHILD_CPU_S
TODAY_SLICE_FRACTION = 0.15      # bot/engines/base.py SLICE_FRACTION
TODAY_TT_ENTRIES = 350000        # bot/engines/search_job.py TT_CAP
TODAY_CACHE_ROOTS = 200          # bot/engines/search.py CACHE_MAX_ROOTS
TODAY_THREADS = 4                # config.PREP_PARALLEL_VARIANTS / CODEGEN_CANDIDATES order of magnitude

DEFAULT_MEM_MB = 2048            # the contract figure: only when nothing can be measured
MIN_RESERVE_MB = 256
RESERVE_FRACTION = 0.20
MIN_CHILD_MEM_MB = 256           # below this a Python child plus a simulator is not safe
MAX_CHILD_MEM_MB = 1024          # a bigger address space buys nothing: every cap in the jobs is finite
NO_LIMIT_MB = 1 << 40            # anything above this is the "no limit" sentinel
MAX_SEARCH_WORKERS = 4
CACHE_FACTOR = 4                 # solved roots kept: prep stores more and the match must not truncate them

PHASES = ("prep", "turn", "idle")
CPU_BOUND = ("turn_search", "prep_verify", "prep_mastery", "prep_solve")
KINDS = CPU_BOUND + ("prep_codegen",)

Plan = namedtuple("Plan", [
    "kind", "phase",
    "children",          # sandbox children this workload may have alive at once
    "child_mem_mb",      # RLIMIT_AS of each child
    "child_cpu_s",       # CPU seconds of a child's whole life (backstop)
    "threads",           # parent threads for model-bound work
    "tt_entries",        # size of an in-child transposition table
    "samples_scale",     # the jobs multiply their sample caps by this
    "search_workers",    # parallel children for one search (<= children)
    "slice_fraction",    # fraction of the move clock for the engine; None = the engine's own default (0.15)
    "cache_roots",       # solved roots kept in solved.json
    "warm_worker",       # keep one child per game between turns (False in idle)
])

# ---------------------------------------------------------------- injectable roots (tests point these at a temp dir)
CGROUP_ROOT = "/sys/fs/cgroup"
PROC_ROOT = "/proc"

_lock = threading.Lock()
_cache = {}
_phase = "turn"


def reset():
    """Forget every cached probe and go back to the default phase. For tests and tools."""
    global _phase
    with _lock:
        _cache.clear()
        _phase = "turn"


# ---------------------------------------------------------------- small readers (never raise)
def _read(path):
    try:
        with open(path, "r", encoding="ascii", errors="replace") as f:
            return f.read().strip()
    except Exception:
        return ""


def _int(text):
    try:
        return int(str(text).strip().split()[0])
    except Exception:
        return None


def _env_int(name, lo=None, hi=None):
    """An explicit BOT_* integer, clamped. None when unset or not a number."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        v = int(float(raw))
    except Exception:
        return None
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


def _env_float(name, lo, hi):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return max(lo, min(hi, float(raw)))
    except Exception:
        return None


def _flag(name, default):
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in ("0", "off", "false", "no", "none"):
        return False
    return raw in ("1", "on", "true", "yes", "all")


def enabled():
    """False when BOT_RESOURCES=off: every plan is exactly today's constants."""
    return _flag("BOT_RESOURCES", True)


def _memo(key, fn):
    with _lock:
        if key in _cache:
            return _cache[key]
    try:
        val = fn()
    except Exception:
        val = None
    with _lock:
        _cache[key] = val
    return val


# ---------------------------------------------------------------- probes
def _meminfo_mb():
    for line in _read(os.path.join(PROC_ROOT, "meminfo")).splitlines():
        if line.startswith("MemTotal:"):
            kb = _int(line.split(":", 1)[1])
            if kb:
                return kb // 1024
    return None


def _cpu_probe():
    counts = []
    try:
        counts.append(len(os.sched_getaffinity(0)))
    except Exception:
        pass
    text = _read(os.path.join(CGROUP_ROOT, "cpu.max"))          # cgroup v2: "quota period" or "max period"
    if text:
        parts = text.split()
        if len(parts) >= 1 and parts[0] != "max":
            q = _int(parts[0])
            p = _int(parts[1]) if len(parts) > 1 else 100000
            if q and p and q > 0 and p > 0:
                counts.append(max(1, -(-q // p)))
    else:                                                       # cgroup v1
        q = _int(_read(os.path.join(CGROUP_ROOT, "cpu", "cpu.cfs_quota_us")))
        p = _int(_read(os.path.join(CGROUP_ROOT, "cpu", "cpu.cfs_period_us")))
        if q and p and q > 0 and p > 0:
            counts.append(max(1, -(-q // p)))
    if not counts:
        try:
            n = os.cpu_count()
            if n:
                counts.append(n)
        except Exception:
            pass
    return max(1, min(counts)) if counts else 2


def _limit_probe():
    total = _meminfo_mb()
    found = None
    for path in (os.path.join(CGROUP_ROOT, "memory.max"),
                 os.path.join(CGROUP_ROOT, "memory", "memory.limit_in_bytes")):
        raw = _read(path)
        if not raw or raw == "max":
            continue
        v = _int(raw)
        if v and v > 0:
            found = v // (1024 * 1024)
            break
    if found is not None and found < NO_LIMIT_MB:
        if total:
            found = min(found, total)        # the "no limit" sentinel is a huge number: clamp against MemTotal
        return max(1, found)
    if total:
        return total
    return DEFAULT_MEM_MB


def _stat_value(path, key):
    for line in _read(path).splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == key:
            return _int(parts[1])
    return None


def _used_probe():
    """Memory in use now, MB, parent plus children. Page cache that the kernel can drop is not counted."""
    cur = _int(_read(os.path.join(CGROUP_ROOT, "memory.current")))
    if cur is not None:
        inactive = _stat_value(os.path.join(CGROUP_ROOT, "memory.stat"), "inactive_file") or 0
        return max(0, cur - inactive) // (1024 * 1024)
    cur = _int(_read(os.path.join(CGROUP_ROOT, "memory", "memory.usage_in_bytes")))
    if cur is not None:
        inactive = _stat_value(os.path.join(CGROUP_ROOT, "memory", "memory.stat"), "total_inactive_file") or 0
        return max(0, cur - inactive) // (1024 * 1024)
    rss_kb = 0
    for line in _read(os.path.join(PROC_ROOT, "self", "status")).splitlines():
        if line.startswith("VmRSS:"):
            rss_kb = _int(line.split(":", 1)[1]) or 0
            break
    kids_kb = 0
    try:
        import resource
        kids_kb = int(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss)     # kB on Linux
    except Exception:
        pass
    return (rss_kb + kids_kb) // 1024


def limits_work():
    """True when `resource` rlimits exist on this platform (the sandbox child treats them as best effort)."""
    def probe():
        import resource
        return hasattr(resource, "RLIMIT_AS")
    return bool(_memo("limits", probe))


# ---------------------------------------------------------------- public numbers
def cpus():
    """Usable cores, >= 1: the minimum of the affinity mask and the cgroup quota. BOT_CPUS wins."""
    v = _env_int("BOT_CPUS", 1, 256)
    if v is not None:
        return v
    return int(_memo("cpus", _cpu_probe) or 2)


def mem_limit_mb():
    """The container's memory limit as detected (BOT_MEM_LIMIT_MB wins)."""
    v = _env_int("BOT_MEM_LIMIT_MB", 256, NO_LIMIT_MB)
    if v is not None:
        return v
    return int(_memo("limit", _limit_probe) or DEFAULT_MEM_MB)


def mem_used_mb():
    """Memory in use now, parent plus children. Read every time (cheap), never raises."""
    try:
        return int(_used_probe())
    except Exception:
        return 0


def reserve_mb():
    """Memory we never allocate: max(256 MB, 20 percent of the limit). BOT_RESERVE_MB wins (floor 128)."""
    limit = mem_limit_mb()
    v = _env_int("BOT_RESERVE_MB", 128, limit)
    if v is not None:
        return v
    return max(MIN_RESERVE_MB, int(limit * RESERVE_FRACTION))


def mem_available_mb():
    """limit - used - reserve, never negative."""
    return max(0, mem_limit_mb() - mem_used_mb() - reserve_mb())


def phase():
    return _phase


def set_phase(name):
    """"prep" | "turn" | "idle". Anything else is ignored. Returns the phase now in force."""
    global _phase
    name = str(name or "").strip().lower()
    if name in PHASES:
        _phase = name
    return _phase


# ---------------------------------------------------------------- plans
def _today(kind, ph):
    return Plan(kind=kind, phase=ph, children=TODAY_CHILDREN, child_mem_mb=TODAY_CHILD_MEM_MB,
                child_cpu_s=TODAY_CHILD_CPU_S, threads=TODAY_THREADS, tt_entries=TODAY_TT_ENTRIES,
                samples_scale=1.0, search_workers=1, slice_fraction=None,
                cache_roots=TODAY_CACHE_ROOTS, warm_worker=False)


def _idle(kind):
    return Plan(kind=kind, phase="idle", children=0, child_mem_mb=TODAY_CHILD_MEM_MB,
                child_cpu_s=TODAY_CHILD_CPU_S, threads=1, tt_entries=TODAY_TT_ENTRIES,
                samples_scale=1.0, search_workers=1, slice_fraction=None,
                cache_roots=TODAY_CACHE_ROOTS, warm_worker=False)


def plan(kind):
    """The only thing callers use. Pure function of the measured limits, the phase and the BOT_* knobs."""
    try:
        return _plan(str(kind or ""))
    except Exception:
        return _today(str(kind or ""), _phase)


def _plan(kind):
    ph = _phase
    if not enabled():
        return _today(kind, ph)
    if ph == "idle":
        return _idle(kind)
    n_cpu = cpus()
    avail = mem_available_mb()
    explicit = _env_int("BOT_MAX_CHILDREN", 1, n_cpu + 1)
    # CPU-bound work never above cpus(), anything never above cpus() + 1. With 1 detected CPU that is 1,
    # which is below today's 2: the limit really is smaller. Else today's 2 is the floor.
    bound = n_cpu if kind in CPU_BOUND else n_cpu + 1
    children = min(explicit, bound) if explicit is not None else bound
    # Memory per child is sized for a pool of n_cpu CPU-bound children, the same for every kind of the
    # phase, so a mix of workloads in one pool can never add up to more than the pool was sized for.
    # Today's 512 MB per child is the floor: when memory is short the NUMBER of children shrinks first.
    pool = explicit if explicit is not None else n_cpu
    fit = max(1, avail // TODAY_CHILD_MEM_MB)
    pool = min(pool, fit)
    children = min(children, fit)
    child_mem = min(avail // max(1, pool), MAX_CHILD_MEM_MB)
    forced = _env_int("BOT_CHILD_MEM_MB", MIN_CHILD_MEM_MB, 4096)
    if forced is not None:
        child_mem = min(forced, max(MIN_CHILD_MEM_MB, avail // max(1, children)))     # clamped to what is safe
    child_mem = max(MIN_CHILD_MEM_MB, child_mem)
    while children > 1 and children * child_mem > avail:
        children -= 1
    # tables grow with the memory a child really has; today's value is the floor
    tt = int(TODAY_TT_ENTRIES * max(1.0, child_mem / float(TODAY_CHILD_MEM_MB)))
    workers = _env_int("BOT_SEARCH_WORKERS", 1, MAX_SEARCH_WORKERS)
    workers = min(children, MAX_SEARCH_WORKERS) if workers is None else min(workers, children)
    if kind != "turn_search":
        workers = 1
    threads = _env_int("BOT_PREP_WORKERS", 1, 32)
    if threads is None:
        threads = max(TODAY_THREADS, n_cpu + 1) if ph == "prep" else n_cpu + 1
    slice_fraction = _env_float("BOT_SLICE_FRACTION", 0.05, 0.40)      # None: the engine's own default
    return Plan(kind=kind, phase=ph, children=children, child_mem_mb=int(child_mem),
                child_cpu_s=TODAY_CHILD_CPU_S, threads=int(threads), tt_entries=tt, samples_scale=1.0,
                search_workers=max(1, workers), slice_fraction=slice_fraction,
                cache_roots=TODAY_CACHE_ROOTS * CACHE_FACTOR,
                warm_worker=_flag("BOT_WARM_WORKER", True) and ph == "turn")


def _apply(ph):
    """Make the sandbox and the engines match the phase. Lazy imports; never raises."""
    try:
        from . import log
        from .prep import sandbox
        if ph == "idle":
            try:
                from .engines import base as engines_base
                engines_base.shutdown_workers()
            except Exception:
                log.exception("resources: warm workers not closed")
            sandbox.kill_all("idle")             # nothing may be alive between games
        else:
            sandbox.set_capacity(max(1, plan("prep_verify" if ph == "prep" else "turn_search").children))
    except Exception:
        pass


def enter(name):
    """set_phase plus: prep/turn size the sandbox slot pool from the plan, idle closes every child.
    Called by main.py when the phase changes. Returns the phase now in force."""
    ph = set_phase(name)
    _apply(ph)
    return ph


class scoped:
    """`with resources.scoped("prep"):` enters a phase and restores the phase AND the sandbox capacity afterwards
    (for code that can be called from tools and tests, which must not leak a capacity into what runs next)."""

    def __init__(self, name):
        self.name = name

    def __enter__(self):
        self.prev = _phase
        try:
            from .prep import sandbox
            self.cap = sandbox.capacity()
        except Exception:
            self.cap = None
        enter(self.name)
        return self

    def __exit__(self, *exc):
        global _phase
        _phase = self.prev
        if self.cap is not None:
            try:
                from .prep import sandbox
                sandbox.set_capacity(self.cap)
            except Exception:
                pass
        return False


def search_workers():
    """Parallel children for one search right now (1 when anything is unclear)."""
    try:
        return plan("turn_search").search_workers
    except Exception:
        return 1


def describe():
    """One flat dict for the log and the metrics. Never raises."""
    try:
        p = plan("turn_search")
        return {"enabled": enabled(), "phase": _phase, "cpus": cpus(), "mem_limit_mb": mem_limit_mb(),
                "mem_used_mb": mem_used_mb(), "reserve_mb": reserve_mb(), "mem_available_mb": mem_available_mb(),
                "children": p.children, "child_mem_mb": p.child_mem_mb, "search_workers": p.search_workers,
                "slice_fraction": p.slice_fraction, "warm_worker": p.warm_worker, "rlimits": limits_work()}
    except Exception:
        return {"enabled": False, "phase": _phase}
