# Overnight report (Sat night into Sun 15 Nov)

Everything below is on `main` and pushed to `origin/main` as `luckydevs-3`, with your permission and your
credentials. Eleven commits, `722a5cf..f561dfc`. `python -m unittest` is green at 558 tests, and the dev key
went from 0.64 to 1.47 USD (4.53 left of 6).

Read the three "please look at this" items first, then the rest as you like.

## 1. Three things that need you

1. **The Dockerfile change is NOT verified by a build.** Docker Desktop's WSL integration went down halfway
   through the night (`docker ps` says "could not be found in this WSL 2 distro") and never came back, so I
   could not build or run the image. The change installs the libraries of `bot/libs/requirements-core.txt` and
   fills `/opt/wheelhouse` from `requirements-extra.txt`. What I did instead: every pin was resolved against
   the real registry, installed into a clean Python 3.12 venv and imported (all ten), and the wheelhouse
   download was run exactly as the Dockerfile runs it (39 MB, three wheels). Every Linux wheel is
   manylinux_2_27 or 2_28, and the image is bookworm (glibc 2.36), so they fit. **Please build once before
   the freeze**: `python scripts/build.py dev && python scripts/check_image.py dev`. If anything goes wrong,
   `--build-arg BAKE_LIBS=0` gives you the old lean image and the engine subsystem simply falls back.
   Size: site-packages comes to about 276 MB (scipy 139, numpy 70), so expect roughly a 400 MB image.
2. **`origin/main` was failing its own test suite before I touched it** (commit `1c39a12`): five tests failed
   because `python -m unittest` imports `bot.policies` while walking the repository, which pulls in
   `bot.config` before `tests/__init__.py` sets `BOT_FAKE_LLM`, so the whole suite ran against a dead HTTP
   endpoint instead of the fake model. Fixed in `807e054`. Worth telling whoever runs the checklist, because
   RULES 25 ("python -m unittest passes") was quietly false.
3. **I pushed `5229ef0` with the suite red** and fixed it immediately in `f561dfc`. The cause was a test of
   mine that replaced `bot.llm.complete` for the whole process. No other commit went out red; I re-ran the
   full suite before each.

## 2. The engine subsystem (the big one, `722a5cf`)

`bot/libs/`: discover -> install -> learn -> generate -> validate -> use. During prep, for an unknown game, it
works out what kind of computation the game needs, finds a general-purpose library for it, installs it in
isolation, reads the API of the installed package, has Haiku write an adapter, and measures how far that
adapter can be trusted before a single move depends on it.

| File | What it does |
|---|---|
| `capabilities.py` | ten capability tags from the profile prep already built; no game name ever leaves this file |
| `net.py` | the only network door: GET only, host allowlist, redirects re-checked, byte and call caps, and queries that can only be built from a fixed capability vocabulary |
| `catalog.json` | the curated library per capability as DATA, with our rank, a size cap, and the deny list of hosted-model clients |
| `discover.py` | every name verified on the PyPI JSON API: wheel for this interpreter, not yanked, size cap, a settled release (it falls back to an older version), maturity for names we did not curate |
| `install.py` | wheels only, wheelhouse before the network, `flock` on /memory, atomic publish, bundles shared between containers, import probe |
| `docs.py` | local introspection of the installed package first (no network, authoritative), then the registry text; BM25-ranked chunks, cached; prompt injections stripped out of untrusted READMEs |
| `adapter_gen.py` | Haiku writes `adapter.py`; our own `ast` check allows only the chosen libraries plus a fixed stdlib list, nothing at import time, no dangerous builtins; up to four rounds |
| `adapter_job.py` | the only place an adapter or a library ever runs: a sandbox child |
| `validate.py` | trust A (agrees with an exact search of our own simulator), B (legal, fast, unproven), C (advisory only), X (rejected) |
| `engines/library.py` | the runtime: inside the move slice, one warm child per game; the trust level decides whether the rows are the move (A/B) or only evidence in the prompt (C) |

Two invariants worth knowing: the bot process never imports a third-party library (only sandbox children do),
and `bot/libs/` imports nothing at all until it is used. `BOT_ENGINE_SUBSYSTEM=off` restores the old behaviour
exactly; with the fake model it is off by default.

**Measured, with the real key.** On `rps_twist` prep found nashpy and scipy, installed what was missing in
1.4 s, read the API by introspection, and Haiku wrote an adapter in one to three rounds; both variants came out
at trust B (no exact cross-check is possible there), and the match played `RESULT: CLEAN`. On `guess_number` it
chose python-sat the same way. One adapter used nashpy for the equilibrium, the other solved the 3x3 game in
plain Python and said so in its docstring - the report records which, instead of claiming a library that is not
used.

**The one finding that matters beyond this feature:** importing numpy or scipy inside a sandbox child *failed*
even with a 1.2 GB `RLIMIT_AS`, because OpenBLAS reserves address space per core. The sandbox child now pins
every BLAS thread count to 1 (`bot/prep/sandbox.py` `CHILD_ENV`), which fixes it at 400 MB and also stops a
library from taking CPU from the other specialists.

## 3. Test games for the four families nobody could play (`e9a2c17`)

The kit's practice games are three think-ahead/guessing games, so four of the seven families had never been
played end to end. New in `testgames/`, each a subclass of `arena.core.Game`, all played by
`tests/test_testgames.py` with random moves:

- `kuhn_bet` - one hidden card each, check/bet/call/fold (Bluff)
- `split_deal` - alternating offers over a shrinking pot, private multiplier per seat (Deals)
- `doc_quiz` - same document and question for both seats; "unknown" is worth more than an invention (Truth)
- `taboo` - a trivial count race whose rules forbid one word in chat; the arena's `on_chat` ends the game (Guardrails)

What playing them found:

- our outgoing filter matched a forbidden subject only as an exact word, so a game banning "banana" would have
  ended on "bananas". Fixed, and "cat" still does not match "category".
- the Truth path works: `doc_quiz` scored 10 of 12 with the real model, answering "unknown" where the document
  is silent, and the engine's enforcement replaced one unsupported answer.
- `taboo` was classified as `guardrails` by prep, and we won both games without ever saying the word.

## 4. The classifier was sending document games to the wrong engine (`396aff9`)

A simultaneous document game scored `read_opponent` 2 and `truth` 2, and the tie was broken **by the alphabet**.
A cold container would have played a Truth game with the mixed-strategy engine. Now a decisive word is worth an
extra point, a tie is broken by what the game is ABOUT, and "higher"/"lower" only votes for guessing when the
sentence is about guessing ("a higher total is better" made Pig look like a guessing game). Measured over every
rules text we have, 18 variants: the code-only evidence now names the right family 18 times out of 18, against
15 before.

## 5. Prompt caching: we were paying full price on every call (`8b81c82`)

`scripts/replay_report.py` showed that of 2.2 million prompt tokens we had sent, **zero** were cached. Measured
against the real gateway: two identical plain-text calls report `cached_tokens: 0`, the same calls with a
`cache_control` block write 4842 tokens and then read them back. The gateway caches only what asks to be cached.

The system message of a decision (rules, playbook, tactics) is identical for every turn of a game, so it is now
sent as one marked block. In a real match every turn after the first read 1476 to 1608 cached tokens, which came
out at 0.000249 USD against 0.000451 for a comparable uncached turn - about 45 percent less on a scored
tie-break. A gateway that refuses content blocks gets one retry as plain text and is never asked again
(`BOT_PROMPT_CACHE=off` disables it).

## 6. The bluff engine now plays, because it was measured (`5229ef0`)

bluff was off by default "until a practice game exists". It exists now, so:

| | engine decides | model decides |
|---|---|---|
| arena practice vs the random bot (with prep, real key) | 5 won, 1 drawn, 1 lost of 7 | 1 won, 1 drawn, 4 lost of 6 |
| headless vs random, no model at all | 23 won, 2 drawn, 15 lost of 40 | - |
| the whole ladder vs random, headless | 11 won, 3 drawn, 6 lost of 20 | - |

Every move answered, none illegal, 0.71 s per move. `L3_DEFAULT` now includes bluff and those numbers are in
the comment next to it. Risk is bounded by what was already there: the engine only runs on a verified simulator
and the live check switches it off for a game as soon as the simulator disagrees with the arena.

## 7. Tools you can use in the morning

```
python scripts/replay_report.py                 # results per family, why we lost, decisions, prep, slide lines
python scripts/replay_report.py --notes NOTES.md
python scripts/demo_page.py                     # results/demo.html: one offline page for the demo
python scripts/engine_arena.py --game kuhn_bet --a code --b random -n 30   # free, no model, seconds
python scripts/influence_report.py              # which chat tactic beat its control
```

What `replay_report` says about the 193 games we have played so far: **zero warnings, zero timeouts, zero
crashes, the watchdog never answered**; 33 percent of turns were answered by the engine, 63 percent by the
model, 5 percent by the safe move; `read_opponent` (rps_twist, 45 percent of 40 games) was the weakest family - see section 11.

`demo_page.py` builds one self-contained HTML page (no network, no script tag, ASCII only): the ladder with our
own shares, every prepared variant, one match move by move with the reason and the chat line of that very step,
and the results per family. To make that honest, the turn metrics line now records the action we really sent,
and the page drops a reason whose recorded action does not match the step.

## 8. /memory under concurrency (`bb1f80f`)

`tests/test_memory_concurrency.py` starts real subprocesses, one per "container", against one directory: no
reader ever sees a half-written file, no temporary file is left behind, each container keeps its own metrics
file intact, the shared solved-roots cache may lose an update (that only costs a cache hit) but never ends up
corrupt, a variant of another harness version is ignored, and the install lock is exclusive between processes.

## 9. Shared files I touched

`bot/config.py` (cache prices and `PROMPT_CACHE_MIN_CHARS`, the subsystem switches), `bot/main.py` (phases for
the subsystem, the action in the metrics line, the library result at `end`), `bot/ladder.py` (L3b, the library
engine), `bot/resources.py` (close adapter children when idle), `Dockerfile`, `tests/__init__.py`,
`bot/llm.py`, `bot/prep/{classify,pipeline,sandbox}.py`, `bot/engines/{base,bluff,search,simultaneous}.py`,
`bot/chat/decide.py`, `READMEFIRST.md`, `GAMEPLAN.md` (ticked what is done, with where to look).

## 10. What I did not do

- **No Docker build** (see item 1). Nothing else is unverified.
- **No prep sweep over every game.** Fresh prep has been run on tic_tac_toe, pig, guess_number, rps_twist,
  nim_deal and kuhn_bet; doc_quiz, taboo and split_deal have only been played from a cold start.
- **The subsystem's exact cross-check only works where our own simulator can prove a position** (sequential,
  perfect information). For guessing and bluff variants the ceiling is trust B. A guessing-specific cross-check
  (against our own guess engine) would raise it; I left it out rather than guess at it.
- **Prep still uses its whole 600 s** because mastery self-play runs to the deadline, so a second prep of the
  same game is not faster, it just does more self-play. That is deliberate (prep CPU is free), but it is not
  the "second prep under 60 s" the brief imagined.
- **`read_opponent` is now fixed and measured** (`aade149`, section 11) - that line in an earlier version of
  this report said I had left it alone.

## 11. `read_opponent`, the weakest family, was weak for a reason (`aade149`)

This is the one piece of play strength I added after writing the rest of this report, so it is at the end.

The family was at 45 percent of 40 games. Two things made it impossible to even look at:

1. `scripts/engine_arena.py` asks `bot/policies/` first, exactly as `bot/ladder.py` does, and
   `bot/policies/rps.py` claims `rps_twist` with 0.98. That is our ONLY `read_opponent` game, so the family
   engine never answered a single turn there: changing its constants changed nothing, and I spent several runs
   getting byte-identical results from five different settings before I noticed the 0.000 s per move.
2. Neither baseline can see an opponent model. Against `random`, every action in a symmetric game has the same
   EV, so the exploitation branch never even fires (RPS against a uniform opponent is a coin flip, and the
   53 percent I measured is the theoretical maximum, not a weakness). Against `first`, one observation is
   enough, so any setting scores 100 percent.

So I added what was missing to measure it: `--no-policies`, which takes the registry out of the way reversibly,
and two opponents in between - `biased` (60 percent of its mass on one action) and `shifty` (the same lean,
moved every 3 turns, which is the case that punishes leaning on a stale pattern).

With the engine actually playing, on `rps_twist` "9 rounds", a draw counting as a half, two seeds, 24-40 games:

| exploitation cap `W_MAX`/`W_HALF` | vs `biased`  | vs `shifty`  |
| --------------------------------- | ------------ | ------------ |
| 0.5 / 8 (what we shipped)         | 0.354, 0.537 | 0.583, 0.633 |
| 0.9 / 4 (now the default)         | 0.729, 0.800 | 0.550, 0.600 |

The old cap was giving away about a quarter of the score against a stable pattern to save about 0.04 against a
moving one, and 0.04 over 30 games is noise. The numbers are written next to the constants in
`bot/engines/simultaneous_job.py`, and both stay overridable per payload (`BOT_EXPLOIT_MAX`,
`BOT_EXPLOIT_HALF`), so retuning needs no code change. The cap itself stays: equilibrium always keeps mass, we
are never a pure best response, and the test that asserts this now reads the constant from the job file instead
of hardcoding 0.5.

Two things worth knowing in the morning:

- **The dedicated policy is still better than the engine here** (0.887 against `biased`, versus 0.800), so
  `bot/policies/rps.py` rightly keeps `rps_twist`. The engine's numbers matter for a `read_opponent` game in
  the tournament that no policy recognises, which is the case we cannot practise against.
- **Anything hidden behind a dedicated policy is unmeasured by default.** `--no-policies` now exists; it would
  be worth running it once per family to see where else a policy is answering for an engine we believe in.

## 12. The game nobody has played: an unknown family now plays at L3 (`7c777bf`)

GAMEPLAN part E says T3 brings nine games nobody has played and that generality decides. The unclassifiable
game was the hole in that, and it was a silent one. Prep DOES build and verify a simulator for a game whose
family it cannot name (`prep/pipeline.py`, `PHASE_B_UNKNOWN_MIN_S`), and then nothing ever asked for it,
because no engine is registered under `"unknown"`: the whole L3 level was skipped and the game was played by a
single model call, with a verified simulator of it sitting unused in `/memory`.

`base.resolve_engine()` now lends such a game the engine that fits the shape of its simulator: the
mixed-strategy engine for a simultaneous step, the search engine otherwise - and the search job already had a
mode for a simulator whose outcomes it cannot score (MCTS instead of alpha-beta), written for exactly this
case and never reached. Measured by forcing the family of a real prepared variant to `unknown`, engine against
random, 16 games, a draw counting as a half:

| game                 | fallback off (what shipped) | on                  |
| -------------------- | --------------------------- | ------------------- |
| `nim_deal` 3 heaps   | 0.250, 46 turns unanswered  | 1.000, 0 unanswered |
| `tic_tac_toe` 3x3    | 0.625, 58 turns unanswered  | 0.938, 0 unanswered |

`"unknown"` is a member of `L3_ALL` and of `L3_DEFAULT`, so `BOT_L3` without it restores the old behaviour
exactly, and a test asserts that. The borrow is safe in both directions: the mixed engine probes the live
simulator and returns nothing when the step is not simultaneous after all, and a job that cannot read the game
answers with an error, which costs that one turn and drops us down the ladder as today.

While looking for other engines nobody asks, I audited every prepared game with the new `--no-policies`
switch. Only two families are shadowed by a dedicated policy: `read_opponent` (section 11) and `guessing`,
where `bot/policies/wordle.py` answers for `guess_number` - and there it is harmless, the policy and the
`GuessEngine` score identically (0.625 and 0.646 on two seeds; the same moves).

## 13. Prep time budget: now recorded instead of argued about

The last open item on my GAMEPLAN line. Every prep we have run since mastery exists takes the whole budget -
498 to 501 s, 13 of the 48 preps in `/memory/metrics` - and the metrics line said nothing about what for,
while the early preps (before mastery) finished in 34 to 67 s. The stages are elastic by design: mastery and
the engine subsystem take whatever the simulators leave. So the budget cannot be tuned from the fractions in
the source, only from what the stages really take under the arena's own clock.

`pipeline.LAST_PREP` now records the split per stage (phase A, simulator chains, pre-solving, the engine
subsystem, mastery; plus the variant counts and the budget), `main.py` puts it on the `prep` metrics line, and
`scripts/replay_report.py` tables it with one line separating what we NEED to play from the elastic stages.
After the round robin, "where did the 509 s go" is a table, for every game, from the replays - which is what
part E asks for. Old prep lines without a split are ignored, not counted.

Two real preps against a COLD memory dir, the arena's own 509 s hard clock, seconds per stage:

| stage       | `kuhn_bet`, 2 variants | `nim_deal`, 4 variants | what it buys                          |
| ----------- | ---------------------- | ---------------------- | ------------------------------------- |
| `phase_a`   | 4.6                    | 7.1                    | profile and family of every variant   |
| `chains`    | 79.2 (2/2 verified)    | 43.5 (4/4 verified)    | candidate simulators and verification |
| `solve`     | 0.0                    | 28.4                   | pre-solved roots                      |
| `libs`      | 151.7 (trust C)        | 60.6 (trust B x4)      | the engine subsystem                  |
| `mastery`   | 263.1                  | 359.1                  | self-play to the deadline             |
| **total**   | **498.6**              | **498.6**              | of a 509 s budget                     |

What that says, and the reason it was worth the two runs:

- **Everything we need in order to PLAY is finished after 84 s (and after 51 s with four variants).** The
  other 83 percent of the budget buys depth. That is the intended shape - prep CPU is free and the arena
  waits for `ready` either way - but it was an assumption until now.
- **Four variants do not strain the budget.** I built a four-variant prep by hand (every game we own has
  exactly two, and the contract does not cap the count) because that is the T3 case we cannot practise:
  phase A took 7 s for four, all four verified in 43 s, and generation never came near its 270 s window. So
  `PREP_PARALLEL_VARIANTS = 4` and `PHASE_B_END_FRACTION = 0.53` need no change.
- **The engine subsystem is where the variance is**: 152 s for trust C on `kuhn_bet`, 61 s for trust B on all
  four `nim_deal` variants. It stops as soon as it has nothing left to try, so it is not wasting the window -
  but on a game where it returns C, that is time mastery would otherwise have had, and C is only evidence in
  the prompt. That is the one number worth watching in the round robin, and it is now in the table.

## 14. The "weakest family" line was sending us to fix a coin flip

While reading the family table after all of this, the number that started section 11 turned out not to be a
measurement at all. `read_opponent` at 45 percent of 40 games is 18 wins: **six tenths of one standard error
below even** (a 40 game run has a standard error of about 8 points). And `rps_twist` is a symmetric zero-sum
game, where an opponent who is merely unpredictable cannot be beaten by more than noise, no matter how good
our engine is - exactly what the `random` baseline in section 11 shows, where 53 percent IS the ceiling.

So the slide line now prints how far below even the family really is and only says "fix it generically" at two
standard errors or more; otherwise it says the result is not yet distinguishable from chance and points at
`scripts/engine_arena.py`, where a family can be measured in seconds instead of in forty games. Both branches
have a test.

This does not undo section 11: the exploitation cap was genuinely costing a quarter of the score against a
readable opponent, and that was measured headless, not inferred from the 45 percent. It does mean the family
was never the emergency the old line implied, and that the next person to read the report will not spend the
morning on one.

I also compared the two players that can own `rps_twist`, since the real games are played by
`bot/policies/rps.py` and not by the engine at all: against `biased` the policy scores 0.883 and 0.783 and the
engine 0.729 and 0.800; against `shifty` the policy 0.550 twice and the engine 0.700 and 0.550. At thirty
games those differences are inside the noise, so I left the policy owning the game rather than change it on a
coin flip.
