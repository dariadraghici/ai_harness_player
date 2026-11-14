# PROMPT-RESOURCES: make the harness use the whole container (2 CPUs, 2 GB RAM)

Hand this file to the coding agent (or agents) that will do the work. It is a
task brief, not documentation of something that exists: nothing described here
is implemented yet. Everything in it is written in English and plain ASCII
(RULES 28).

---------------------------------------------------------------------------

## 0. Mission

Our harness today leaves most of the container idle. The arena gives every
container 2 CPUs, 2 GB RAM and 256 processes (docs/ARENA-CONTRACT.md 2.1), and
prep gives us 600 seconds of CPU time that costs us nothing in the score.
We currently use about one core and roughly a quarter of the RAM.

Your job: make every phase of the bot scale its own parallelism and its own
memory use to the resources it actually has, measured at run time, while
keeping every bot invariant in RULES.md intact. More CPU and more RAM must
never turn into a warning, a timeout, a crash or an OOM kill: those cost
1.25 robustness points each (kit note K11) and lose the game.

Two framing facts that decide every trade-off:

1. CPU and RAM are FREE in the score. Cost is measured only in model spend
   during matches (`cost = 2.5 x cheapest average match spend / ours`,
   K11), and prep spend does not count at all. So: move work from the model
   to our own code, and move work from the match into prep.
2. WALL CLOCK is scored. Total answer time is the second tie-break of a
   knockout match (ARENA-CONTRACT 2.4). Using two cores to answer the same
   move in half the time is worth points; using two cores to think twice as
   long for the same move is not, unless it changes the move.

So the goal is not "burn more CPU". The goal is: same or better move, less
wall clock per answer, and all the heavy thinking paid for in prep.

## 1. Read before you touch anything

In this order: `RULES.md`, `READMEFIRST.md`, `docs/ARENA-CONTRACT.md` (kit
notes K1 to K14 first), then `GAMEPLAN.md` part A. Then the files listed in
section 3 of this brief.

Non-negotiable invariants (RULES 14 to 24). Any of these broken by a
performance change is a defect, not a trade-off:

- only `bot/protocol.py` writes to stdout; stdout stays byte-exact;
- the bot never exits and never raises to the top level; every new code path
  falls down the ladder instead of failing;
- every answer respects the deadlines in `bot/config.py`; long computation
  checks its deadline or runs in a killable child process (a thread cannot be
  killed, `bot/clock.py` says so);
- only the gateway's `haiku` through `bot/llm.py`; no other model, ever;
- standard library only. No new dependency without Sebi (RULES 23).
  `threading`, `concurrent.futures`, `multiprocessing`, `subprocess`,
  `resource`, `os.sched_getaffinity`, `ctypes` are all standard library and
  allowed; prefer the ones already used in the repo;
- no game-specific code (RULES 22). Scaling rules are per family and per
  measured resource, never `if game_id == ...`;
- an idle specialist must use almost no CPU and little memory
  (ARENA-CONTRACT 2.1): several of our containers are alive at the same
  time. This is the hard constraint on everything below;
- 256 processes per container. Every child is counted, bounded and killed.

Ownership (RULES 10, 11). The work splits like this; do not edit outside your
area, and do not edit a shared file without Sebi's permission:

| Work package | Owner | Shared files involved |
|---|---|---|
| WP0 resource budget module | luckydevs-1 | new `bot/resources.py` is new, but `bot/config.py` constants need Sebi |
| WP1 sandbox scaling | luckydevs-3 | none |
| WP2 engine worker reuse and parallel search | luckydevs-3 | none |
| WP3 turn pipeline and slices | luckydevs-3 + Sebi | `bot/ladder.py`, `bot/config.py` |
| WP4 prep saturation | luckydevs-3 | `bot/config.py` (PREP_* constants) |
| WP5 cache and table sizing | luckydevs-3 | none |
| WP6 measurement | luckydevs-1 | `bot/main.py` hook (ask Sebi) |
| WP7 local tooling | luckydevs-1 | none |
| WP8 image and env knobs | Sebi | `Dockerfile` |

Branch per work package, from the latest `origin/main`, named
`<name>-<feature>` (RULES 1, 2). Never touch `main` (RULES 3). Add every new
file to `READMEFIRST.md` in the same commit (RULES 12).

## 2. What the container gives us, and what we may take

Per container: 2 CPUs, 2 GB RAM, 256 processes. Several specialists of ours
are alive at the same time during a tournament, so "use all 2 GB" is only
true for the phase where this container is the one doing work.

Treat the resources as three phases, and make the code ask which phase it is
in instead of hard-coding one number for all of them:

| Phase | What runs | CPU we may take | RAM we may take |
|---|---|---|---|
| `prep` | one prep container, 600 s, no match in flight | all of it (oversubscribe slightly: cores + 1 worker threads for model-bound work) | most of it, minus a hard safety margin |
| `turn` | one 20 s move, other containers of ours may be mid-move too | all of it for the length of the slice | a bounded working set, released at the end of the turn |
| `idle` | between games, waiting for `start` | nothing: no child alive, no timer loop | the resident minimum: no warm caches, no retained children |

The safety margin is the point of the whole exercise and the easiest thing to
get wrong. If the sum of what the parent holds plus what the children may
allocate crosses the cgroup limit, the kernel OOM-kills a process in the
container. If it kills the bot, we lose the game and the container, and the
next game of that kind is played by a cold container (K7). So:

- never size a limit from the 2 GB figure in the contract. MEASURE the limit
  at run time and size from what is actually free;
- keep a reserve that is never allocated by us: at least 256 MB, or 20 percent
  of the detected limit, whichever is larger;
- a child that wants too much must die of its own `RLIMIT_AS` (a clean
  `MemoryError` that `bot/prep/sandbox.py` already turns into a
  `SandboxError`), never of the cgroup OOM killer. That means:
  `sum(child RLIMIT_AS) + parent working set + reserve <= detected limit`;
- RSS, not address space, is what the cgroup counts. `RLIMIT_AS` caps virtual
  address space and is therefore the conservative knob; keep using it, and
  additionally watch real usage (see WP6).

## 3. Where the limits are hard-coded today (measured in this tree)

Fix these in place; do not invent a second set of constants somewhere else.

| Value | Today | File |
|---|---|---|
| sandbox children alive per process | `MAX_CHILDREN = 2` | `bot/prep/sandbox.py:57` |
| child address space | `CHILD_MEMORY_MB = 512` | `bot/prep/sandbox.py` |
| child CPU seconds (backstop) | `CHILD_CPU_S = 120` | `bot/prep/sandbox.py` |
| child open files | `RLIMIT_NOFILE 64` | `bot/prep/sandbox_child.py` |
| engine time slice | `SLICE_FRACTION = 0.15` (3 s of 20) | `bot/engines/base.py` |
| engine child lifetime | one FRESH child per turn, killed on return | `bot/engines/base.py` |
| alpha-beta transposition table | `TT_CAP = 350000` | `bot/engines/search_job.py` |
| solved roots kept | `CACHE_MAX_ROOTS = 200` | `bot/engines/search.py` |
| guessing hypotheses / seeds | `MAX_HYPOTHESES = 2000`, `SAMPLE_SEEDS = 40000` | `bot/engines/guess_job.py` |
| read_opponent iterations | `RM_ITERS = 6000`, `SAMPLES_CAP = 400` | `bot/engines/simultaneous_job.py` |
| bluff worlds / samples | `MAX_WORLDS = 240`, `SAMPLES_CAP = 4000` | `bot/engines/bluff_job.py` |
| prep variants in parallel | `PREP_PARALLEL_VARIANTS = 4` | `bot/config.py` |
| codegen candidates | `CODEGEN_CANDIDATES = 3` | `bot/prep/codegen.py` |
| candidates verified | `VERIFY_MAX_CANDIDATES = 4` | `bot/prep/verify.py` |
| mastery self-play workers | `min(2, len(eligible))`, `ROUND_S = 18` | `bot/prep/mastery.py` |
| turn deadlines | `TURN_SOFT_S = 6`, `TURN_HARD_S = 13`, `LLM_MATCH_TIMEOUT_S = 8` | `bot/config.py` |

Two structural facts to keep in mind while reading them:

- `bot/ladder.py` `decide_turn` runs L3 and then L2 strictly in sequence: up
  to 3 s of engine (one core, one child) and then up to 8 s of model call
  (no CPU at all, pure network wait), inside a 13 s hard deadline. During the
  model call both cores are idle. During the engine slice the second core is
  idle;
- `bot/prep/sandbox.py` allows 2 children for the whole process, so prep's
  parallel chains (`run_chains`, up to one per variant), the 4 verification
  jobs per variant and the mastery rounds all queue through the same 2 slots,
  while the parent threads that wait on model calls use no CPU.

Nothing in the tree reads `os.cpu_count()`, `os.sched_getaffinity` or any
cgroup file today. That is WP0.

---------------------------------------------------------------------------

## WP0. `bot/resources.py`: one source of truth (owner: luckydevs-1)

New module, standard library only, never raises, results cached after the
first probe. This is the module every other work package asks.

Detect, in this order, and fall back silently:

- CPUs: `len(os.sched_getaffinity(0))`, then cgroup v2 `/sys/fs/cgroup/cpu.max`
  (`quota period`, quota `max` means unlimited), then cgroup v1
  `cpu.cfs_quota_us` / `cpu.cfs_period_us`, then `os.cpu_count()`, then 2.
  Take the minimum of what the affinity mask and the quota allow, floor 1;
- memory limit: cgroup v2 `/sys/fs/cgroup/memory.max`, then v1
  `/sys/fs/cgroup/memory/memory.limit_in_bytes`, then `/proc/meminfo`
  `MemTotal`, then 2048 MB. Ignore absurd values (the "no limit" sentinel is a
  huge number: clamp against `MemTotal`);
- memory in use now: cgroup `memory.current` / `memory.usage_in_bytes`, else
  `/proc/self/status` `VmRSS` plus children via
  `resource.getrusage(RUSAGE_CHILDREN).ru_maxrss`;
- whether `resource` limits work at all (they do not on every platform; the
  sandbox child already treats them as best effort).

Public API (keep it this small):

```python
cpus()                      -> int    # usable cores, >= 1
mem_limit_mb()              -> int    # the container's limit as detected
mem_used_mb()               -> int    # now, parent plus children
mem_available_mb()          -> int    # limit - used - reserve, never negative
reserve_mb()                -> int    # max(256, 20 percent of the limit)
phase()                     -> str    # "prep" | "turn" | "idle"
set_phase(name)                       # called by main.py / pipeline
plan(kind)                  -> Plan   # the only thing callers use
describe()                  -> dict   # for logs and metrics, one line
```

`plan(kind)` returns a small immutable record for a named workload, for
example `plan("prep_verify")`, `plan("turn_search")`, `plan("prep_mastery")`,
with the fields: `children` (how many sandbox children this workload may have
alive), `child_mem_mb`, `child_cpu_s`, `threads` (parent threads for
model-bound work), `tt_entries` (how big an in-child table may be),
`samples_scale` (a float the jobs multiply their sample caps by).

Rules the planner must obey:

- `children * child_mem_mb <= mem_available_mb()`, and `children` never
  exceeds `cpus()` for CPU-bound work (`turn_search`, `prep_verify`,
  `prep_mastery`), never exceeds `cpus() + 1` for anything;
- `child_mem_mb` has a floor of 256 MB (below that a Python child plus a
  simulator is not safe) and a ceiling of `mem_available_mb() // children`;
- in phase `idle`, every plan is zero children and nothing is kept warm;
- never return a plan that is worse than today's hard-coded numbers unless the
  detected limits really are smaller. Today's values are the FLOOR, not the
  target;
- a single kill switch: `BOT_RESOURCES=off` makes every plan return exactly
  today's constants (2 children, 512 MB, 0.15 slice, today's caps). This is
  what we flip if anything misbehaves minutes before a freeze;
- all other knobs are `BOT_*` too, because only `LLM_*` and `BOT_*` reach the
  bot in the practice arena (ARENA-CONTRACT 2.3):
  `BOT_CPUS`, `BOT_MEM_LIMIT_MB`, `BOT_RESERVE_MB`, `BOT_MAX_CHILDREN`,
  `BOT_CHILD_MEM_MB`, `BOT_SLICE_FRACTION`, `BOT_SEARCH_WORKERS`,
  `BOT_WARM_WORKER` (on/off), `BOT_PREP_WORKERS`. An explicit `BOT_*` value
  always wins over detection and is clamped to what is safe.

Acceptance: unit tests with fake cgroup files (a temp directory injected via a
module-level path, not monkeypatching `open`) prove the detection order, the
clamps, the floors, the `idle` zeroing, `BOT_RESOURCES=off` equivalence with
today's constants, and that every probe failure path returns the fallback
instead of raising. `describe()` is logged once at startup.

## WP1. Scale the sandbox (owner: luckydevs-3)

`bot/prep/sandbox.py` keeps its guarantees word for word (the five in its
docstring). What changes is that the slot count and the child limits come from
`resources.plan(...)` instead of the two module constants.

- replace the module-level `threading.BoundedSemaphore(MAX_CHILDREN)` with a
  small slot pool whose capacity can be set once per phase
  (`sandbox.set_capacity(n)`), called by the prep pipeline and by `main.py`
  when the phase changes. Shrinking must never kill a running child: it only
  stops new ones from starting. Keep the semantics of `SandboxError("busy")`;
- `Worker(path, mem_mb=..., cpu_s=...)` callers stop passing literals and pass
  the plan's values;
- keep `MAX_CHILDREN`, `CHILD_MEMORY_MB`, `CHILD_CPU_S` as the fallback
  defaults the plan returns when detection fails or `BOT_RESOURCES=off`;
- `kill_all()` and `live_count()` stay exactly as they are; they are our proof
  that no child survives a deadline;
- the child's own `set_limits` in `bot/prep/sandbox_child.py` gets the memory
  figure from the command line as it does now, so nothing changes there except
  that `RLIMIT_NOFILE` may need to grow if a workload opens more files. Do not
  grow it otherwise.

Acceptance: `tests/test_sandbox.py` keeps passing unchanged, plus new cases:
capacity 1 and capacity 4 both bounded exactly (no 5th child ever alive),
capacity change under load leaves no orphan, a child that allocates past its
limit dies with `MemoryError` and the parent survives, and `live_count()` is 0
after every test.

## WP2. Stop paying for a cold child every turn, and use both cores (owner: luckydevs-3)

Today `bot/engines/base.py` starts a fresh child per turn, loads the simulator
into it, searches for 3 s and kills it. The start plus load is paid 20 to 40
times per game for nothing, and only one core ever works.

Two changes, in this order:

1. WARM WORKER PER GAME. Keep one `Worker` per (variant, game) alive while a
   game is being played, reuse it for every turn, and kill it at `end` and
   whenever the phase goes `idle`. The docstring invariant becomes: "no child
   is alive once the game is over or the container is idle". Wire the teardown
   into `main.py` `on_end` and `shutdown` (that hook exists; the one-line
   change in `main.py` needs Sebi's permission) and make it belt and braces:
   an idle watchdog in the engine layer that kills a warm worker that has not
   been used for longer than one move clock. A stale warm child must never be
   the reason a specialist is not idle-cheap. The live check stays per turn:
   reusing the child does not mean trusting the simulator longer.
2. PARALLEL SEARCH over `cpus()` children for the think_ahead family, and over
   independent sample batches for guessing, read_opponent and bluff. Use the
   shape that fits each job:
   - think_ahead: root split. The parent asks the job for the root's legal
     actions, partitions them across N children with the same budget, and
     merges by value. With one child it must produce exactly today's result;
   - guessing, bluff: the sample loops are already seeded; give each child a
     disjoint seed range and merge the counts. The merge must be
     ORDER-INDEPENDENT and seed-stable: `bot/engines/simultaneous.py` and
     `bot/engines/bluff.py` draw our action from a mixed strategy seeded by a
     per-process salt, the game and the turn, and that draw must stay
     reproducible. Merge sorted by action, never by completion order, and sum
     in a fixed order. A parallel engine that returns a different move
     depending on which child answered first is a bug;
   - read_opponent: split `RM_ITERS` into independent runs with different
     seeds and average the strategies, or keep one child and spend the second
     core on the payoff-matrix playouts. Measure both, keep the better one.
   The budget per child stays the SLICE, not the slice times N: parallelism
   buys resolution, not more wall clock.

Acceptance: `tests/test_engines.py` keeps passing. New cases: with
`BOT_SEARCH_WORKERS=1` every engine returns bit-identical candidates to the
sequential code on the fixture simulators; with 2 workers the same move is
returned for the fixture positions and the seeded draw is unchanged; a child
that loops or dies mid-search degrades to the candidates the other child
produced and leaves no child alive; `live_count()` is 0 after `end`.
Report in the hand-over: nodes or playouts per second with 1 and with 2
workers, and the measured child start cost we stopped paying.

## WP3. Overlap the turn, then give the engine the time it gains (owner: luckydevs-3, needs Sebi for the shared files)

In `bot/ladder.py` the 8 s model call and the 3 s engine slice are strictly
sequential, which is why a 13 s hard deadline feels tight. The model call
needs the candidates, so it cannot simply be moved earlier. What CAN be
overlapped, and must be:

- everything that does not depend on the candidates: `defense.build_context`,
  the prompt assembly in `bot/chat/decide.py`, the playbook and variant text,
  the opponent profile read, the guardrails tables. Build the prompt skeleton
  in a parent thread WHILE the engine children work, and splice the candidate
  block in at the end. This is pure latency, no extra model spend;
- the L1 safe move is already computed first and reported through `progress`;
  keep that exactly as it is. It is what the watchdog sends;
- do NOT add a second speculative model call to save time. Model spend is
  scored, CPU is not (section 0). One decision, one call (RULES 19,
  ARENA-CONTRACT 2.5).

Then, and only after WP2 has numbers, re-tune the clocks in `bot/config.py`
with Sebi: `SLICE_FRACTION` (0.15 today), `TURN_SOFT_S` (6), `TURN_HARD_S`
(13), `LLM_MATCH_TIMEOUT_S` (8). The rule for the new values: the engine may
grow its slice only as far as the measured p99 of (slice + model call +
finalize) stays under `TURN_HARD_S` with room for one retry after a warning,
and only where a bigger slice measurably changes the move (log how often the
extra time changed the chosen action; if it never does, keep the slice small
and bank the answer time instead, because answer time is a tie-break).

Acceptance: `python scripts/practice.py --game <each game>` prints
`RESULT: CLEAN`, and the metrics show a lower mean and p99 answer time than
the same games on the current `main`, with the same or better win rate over a
`scripts/batch_runner.py` run. Numbers in the hand-over, per game.

## WP4. Saturate prep (owner: luckydevs-3)

Prep is 600 s of free CPU and free model spend. Today most of it waits.

- `bot/prep/pipeline.py` `run_chains` already runs one thread per variant;
  raise the sandbox capacity to the `prep_verify` plan for the whole prep
  phase, so the chains really do run `cpus()` children at once instead of
  queueing through 2 slots, and lower it again before `ready` is answered;
- `bot/prep/verify.py`: `VERIFY_MAX_CANDIDATES` and the repair pool size come
  from the plan. More candidates verified in parallel is a strictly better
  winner choice; the differential test benefits most;
- `bot/prep/codegen.py`: `CODEGEN_CANDIDATES` is model-bound, not CPU-bound.
  Scale it by the plan's `threads` and by the TIME left, not by the cores.
  Mind the dev key's 60 requests per minute when testing locally (RULES 26);
  arena keys have no per-minute limit (K8);
- `bot/prep/mastery.py`: `workers = min(2, len(eligible))` and `ROUND_S = 18`
  become plan-driven, and self-play keeps running until
  `safe_deadline(deadline)`, which it already respects. Prefer many short
  rounds to few long ones so the deadline is never the thing that kills a
  round;
- NEW and most valuable: spend the spare prep CPU on work the match will not
  have to do. Solve what is solvable and store it:
  - for think_ahead variants small enough to solve, extend the
    `solved.json` cache from "roots we happened to search" to "the opening
    positions reachable in the first few plies", written through the existing
    `bot/engines/search.py` cache (`CACHE_MAX_ROOTS`, raise it from the plan).
    A root that is in the cache costs 0 ms and 0 tokens in the match;
  - keep the entries keyed to the simulator hash as they are now, so a
    repaired simulator invalidates them;
  - never store anything derived from the prep sample deal as a fact of the
    game (RULES 30, K2). A cached root is keyed by the canonical state text,
    which is deal-specific by construction: that is fine. A playbook sentence
    that says "the heaps are 3 5 7" is not. Review every new stored artifact
    against this rule before you write it;
  - anything new that prep stores means `HARNESS_VERSION` must be bumped
    (RULES 29, ask Sebi) or old cached specialists will be loaded.

Acceptance: prep still answers `ready` well inside `PREP_HARD_S` on every
practice game, including `--game nim_deal --prep` (RULES 25); the verification
pass rate per game does not drop; the hand-over reports, for each practice
game, verified variants, candidates verified, self-play games, cached solved
roots, and the wall clock of each phase, before and after.

## WP5. Size the tables and sample caps from the plan (owner: luckydevs-3)

`TT_CAP`, `MAX_HYPOTHESES`, `SAMPLE_SEEDS`, `RM_ITERS`, `SAMPLES_CAP`,
`MAX_WORLDS`, `CACHE_MAX_ROOTS` are all guesses made for a 512 MB child. The
job files are standalone (they may not import `bot`), so pass the plan's
`tt_entries` and `samples_scale` in the job PAYLOAD, with the current values as
the default when the field is missing. Keep every cap finite: a table that
grows until `RLIMIT_AS` is reached turns a good position into a
`SandboxError`. Measure per family what a bigger table actually buys (nodes
per second, solved depth, hypotheses kept) and keep only the increases that
show up in the move or in the time.

Acceptance: fixture tests prove the payload default equals today's constant;
no job ever dies of `MemoryError` in the test suite or in a full
`batch_runner` run; measured effect per family in the hand-over.

## WP6. Prove it (owner: luckydevs-1)

Resource claims without numbers are worth nothing to us and nothing to the
judges (harness design is 25 points and they read the source).

- `bot/metrics.py`: add to each decision line the peak RSS of parent and
  children (`resource.getrusage(RUSAGE_SELF).ru_maxrss` and
  `RUSAGE_CHILDREN`), `sandbox.live_count()` at the peak of the turn, the
  number of search workers used, the CPU time of the turn
  (`time.process_time()` delta plus the children's), and the wall clock split
  into L3, L2 and finalize. Keep it one JSON line per decision, same file,
  same shape otherwise;
- log `resources.describe()` once at startup, at info level;
- add an idle check to the pre-push checklist in `scripts/check_image.py`:
  after a finished game and before the next `start`, the container has no
  sandbox child alive and its RSS is back near the baseline. This is the
  direct test of the ARENA-CONTRACT 2.1 sentence about idle specialists;
- extend `scripts/batch_runner.py` to print the new columns (peak RSS,
  children, CPU seconds per match, answer-time p50 and p99) so a change can be
  compared against `main` in one table.

## WP7. Local tooling may use the dev machine fully (owner: luckydevs-1)

This is about our own iteration speed, not the arena.

- `scripts/batch_runner.py`: run matches in parallel with a worker pool sized
  from the HOST cpu count (not the container plan), default `--jobs` of
  `max(1, hostcpus - 1)`, and make it refuse to parallelize real-key runs
  beyond what the 60 requests per minute of the dev key allows (RULES 26).
  With `--fake` there is no budget risk, so that is the default for batches;
- keep every match's artifacts in its own result folder so parallel runs do
  not interleave logs or replays, and keep separate `MEMORY_DIR` roots per
  worker so parallel practice runs do not fight over the same cached
  specialist (the fake and real split by `HARNESS_VERSION` already exists);
- `python -m unittest` stays the entry point and stays green. If you add a
  parallel test runner, it is an extra script, not a replacement.

## WP8. Image and environment (owner: Sebi)

Only after WP0 to WP6 are measured, and only if the numbers ask for it:

- the `Dockerfile` sets `PYTHONDONTWRITEBYTECODE=1`. The sandbox child already
  runs with `-B` on purpose (a repaired candidate must not load a stale
  `.pyc`), so leave that alone; if bytecode caching for OUR modules turns out
  to be worth the cold-start milliseconds, that is a separate, carefully
  argued change;
- consider `PYTHONMALLOC` and `MALLOC_ARENA_MAX=2` only with before and after
  RSS numbers;
- no new base image, no new package, no `pip install` (RULES 23).

---------------------------------------------------------------------------

## 4. Traps, in the order they will bite you

1. The GIL. Threads do not give CPU parallelism for our Python computation.
   Only child processes do. Threads are still right for model calls and for
   controlling children (one controller thread per `Worker`, as the sandbox
   docstring requires).
2. An OOM kill is a lost game AND a lost container. See section 2: always
   leave a reserve, always size from measured free memory, always let
   `RLIMIT_AS` fire first.
3. The 256-process limit and the idle rule. Every parallel child must be
   accounted for, bounded by the slot pool, and dead before `idle`. Grep your
   own diff for every `Worker(` and prove each one is closed on every path,
   including exceptions and deadlines.
4. Determinism. Our mixed-strategy draws are seeded deliberately and must not
   depend on scheduling (WP2). Any merge of parallel results must be a
   function of the results, not of their arrival order.
5. The watchdog is the contract, not a safety net you may lean on. If the
   parallel path makes the watchdog fire, the change is wrong, even when the
   answer is still legal: a watchdog answer is the best-so-far, usually L1.
6. Shared `/memory`. It is one directory for all our containers at once, with
   concurrent writers. More parallelism means more concurrent writes: keep
   every write going through the atomic helpers in `bot/memory.py`, and never
   read-modify-write a shared JSON file without accepting that another
   container may have written it in between (`solved.json` and `live.json` are
   exactly this case).
7. `HARNESS_VERSION` (RULES 29). Any change in what prep stores invalidates
   cached specialists. Bump it with Sebi, or delete the local `memory/` folder
   and wonder why nothing reproduces.
8. Deal independence (RULES 30, K2). Precomputing more in prep makes it much
   easier to accidentally bake the sample deal into a stored artifact. Every
   new stored field gets this question asked out loud in the hand-over.
9. The practice arena passes only `LLM_*` and `BOT_*`. A knob named anything
   else does not exist in a practice match, which is exactly where you will
   be measuring.
10. Platforms without `resource` or without cgroup files (a teammate's macOS,
    WSL). Detection must degrade to the fallbacks and the suite must pass
    there too; `bot/prep/sandbox_child.py` already treats limits as best
    effort and says so.

## 5. Definition of done

A work package is ready to hand over when all of this holds:

- `python -m unittest` passes with the participant kit present, so the
  arena-lite tests are not skipped (RULES 25);
- `python scripts/practice.py --game <g>` prints `RESULT: CLEAN` for
  `tic_tac_toe`, `pig`, `guess_number`, `rps_twist`, `nim_deal`, and
  `--game nim_deal --prep` as well when prep or the start path was touched;
- `python scripts/check_image.py dev` passes, including the new idle check;
- a `scripts/batch_runner.py` table of this branch against `main`: wins,
  warnings, timeouts, answer time p50 and p99, model cost per match, peak RSS,
  children, CPU seconds. No regression in warnings, timeouts or cost;
- `BOT_RESOURCES=off` reproduces today's behavior, and the before and after
  numbers for that switch are in the hand-over;
- every new file has its line in `READMEFIRST.md`, in the right owner section,
  in the same commit (RULES 12);
- the hand-over message says: branch, what changed, which files, how it was
  tested, the measured numbers, and what is risky (RULES 8). Shared files
  touched are called out separately for Sebi.

## 6. Out of scope

Do not, as part of this work: add a dependency; add a second model call per
turn; add game-specific branches; raise `max_tokens` or the thinking budget to
"use more resources" (that is spend, and spend is scored); change the protocol
or the ladder's levels; keep a child alive across games; or weaken any check in
`bot/prep/verify.py` to make a bigger parallel batch pass.

If a step here conflicts with `RULES.md`, stop and ask your human, who asks
Sebi. The rule wins.
