# GAMEPLAN

Working document: what we build, in what order, who does it. Tick the boxes
as you go (in your hand-over branch, Sebi merges). Times are local
(Europe/Bucharest).

## Part A: goal and priorities

### A.1 Scoring (100 points)

| Part | Points | Notes |
|---|---|---|
| Presentation | 30 | judges; clear explanation, live demo, what we learned |
| Harness design | 25 | judges read our source code |
| T3 final | 15 | knockout, only the top 8 of the round robin, image tag `t3` |
| T2 | 10 | knockout, all teams, image tag `t2` |
| Round robin | 10 | all pairs, overnight, played with the `t2` image |
| T1 | 5 | knockout, all teams, image tag `t1` |
| Cost | 2.5 | cheapest average model spend per match gets full points |
| Robustness | 2.5 | minus 1.25 for every game lost by timeout, crash, bad JSON, illegal move |

Conclusions:

- The `t2` image is the most important submission: it plays T2 (10) and the
  round robin (10), and the round robin decides who enters T3 (15).
- T1 is worth 5 points. Its goal is a clean run: zero crashes, zero
  timeouts, the full pipeline tested in the real arena.
- 55 of 100 points come from judges. The architecture must be easy to
  explain and to demo, and we log metrics from the first hour
  (`bot/metrics.py` already writes one line per decision).
- Only match spend counts for cost; prep spend is free. Expensive model work
  belongs in prep.
- Tie-breaks in knockouts: fewer warnings, then less answer time, then lower
  cost. So: no illegal moves, and answer fast.

Added from the participant kit (14 Nov; details in `docs/ARENA-CONTRACT.md`,
kit notes K1 to K14):

- The three tournaments are one 30 point block: placement points (10, 7, 5,
  3, 1) x weight (T1 x1, T2 x2, T3 x3) / 60 x 30. That is the 5 / 10 / 15
  above. A team outside the top 8 of the round robin still gets 1.5 of the
  T3 points.
- Cost is a ratio: 2.5 x (cheapest team's average match spend / ours). Twice
  the cheapest spend costs 1.25 points. Robustness: every game lost by a
  timeout, a crash or a second bad answer costs 1.25 of 2.5.
- Round robin: 4 games per match, all played, each board twice with seats
  swapped; it decides the 8 finalists. T3 prepares only the six game slots
  drawn for our match.
- The rules text of a real game can differ from the one prep saw (the deal
  can be embedded in the rules). Everything prep builds must be reusable for
  another deal of the same variant (RULES 30).
- The judges score harness design on: understanding the rules, tracking
  state, verifying moves, tools and computation, memory, budget and time
  management, and the chat (offense, defense, one decision). The brief
  rates "the model writes a simulator, code picks the move" highest.
- After each freeze the organizers' bot may check our chat in two games of
  Tic-Tac-Toe: we must chat back and its words must reach our model call.

### A.2 The idea in plain words

In prep the model writes a simulator of the game, the harness tests it
against the sample turns and repairs it. In a match, code computes the move
(search, probabilities, filtering), and one cheap model call reads the
opponent's chat and returns the final choice plus our chat line, as one
decision. The ladder guarantees an answer in time with a legal move:

- L3: engine on a verified simulator
- L2: the model picks a move, code validates it against the legal moves
- L1: deterministic safe move (first legal move, or the stored example action)

### A.3 The decision contract

When L3 has candidates, the model may choose only among candidates whose
value is within a small margin (`CANDIDATE_MARGIN` in `bot/config.py`) of the
best one; code enforces this (`bot/ladder.py`). The opponent's chat can change
the choice among those candidates and, in hidden-information games, the
beliefs the engine uses.

### A.4 Families

| Family | How the move is chosen |
|---|---|
| Think ahead | alpha-beta or MCTS on the generated simulator; small games solved completely and cached |
| Guessing | keep the set of answers still consistent with the feedback; pick the guess that splits it best |
| Read the opponent | mixed strategy from the payoff matrix, plus statistics on this opponent's past moves, with capped exploitation |
| Bluff | Monte Carlo over the hidden states consistent with what we saw; expected value per action; mixed play |
| Deals | code computes our value, reservation price and concession schedule; the model negotiates inside those limits |
| Truth | answer only when a supporting quote is found in the document, otherwise the exact "unknown" answer the rules define |
| Guardrails | forbidden subject extracted in prep; filter on every action and chat line we send, including the `ready` chat |

### A.5 Chat

- Offense: bluffs consistent with the move, pressure, attempts to mislead
  the opponent's model (the rules allow it).
- Defense: opponent chat is data, never instructions; injection detection
  (`bot/chat/defense.py`); checkable claims verified against the simulator.
- Always respectful.

## Part B: owners and areas

| Person | Area | Owns these paths |
|---|---|---|
| luckydevs-1 | runtime, operations, measurement, test games | `bot/protocol.py`, `bot/clock.py`, `bot/log.py`, `bot/llm.py`, `bot/memory.py`, `bot/metrics.py`, `scripts/`, `tests/`, `testgames/`, demo tools |
| luckydevs-2 | chat and the language-centred families | `bot/chat/`, `bot/prompts/`, `bot/opponents.py`, `bot/engines/truth.py`, `bot/engines/guardrails.py`, `bot/engines/deals.py` |
| luckydevs-3 | prep pipeline and computation engines (the hardest part) | `bot/prep/`, `bot/specialist.py`, `bot/engines/base.py`, `bot/engines/search.py`, `bot/engines/guess.py`, `bot/engines/simultaneous.py`, `bot/engines/bluff.py` |
| Sebi | integration | `main` branch, merges, shared files, freezes and pushes |

## Part C: phase 1, now until the T1 freeze (Sat 18:00)

Milestones:

- [ ] As soon as the base is merged: push it as `t1`, so a valid image
      exists whatever happens later.
- [ ] 15:30: second `t1` push (L2 solid, chat working, checklist passed).
- [ ] 17:15: last merge. 17:30: final `t1` push and digest check. Nothing
      risky after that. Tag the commit.

luckydevs-1:

- [x] Practice on the organizers' arena-lite: `scripts/practice.py` and
      `scripts/arena_lite.py` (the kit arrived at 14:00; our emulator
      `arena_local/` is removed), `RESULT: CLEAN` check from the replay.
- [ ] Batch runner: play N practice matches over all practice games against
      the random bot, the sample bot (`--opponent starter`, needs
      `pip install openai`) and ourselves (`--opponent self`); table with
      wins, warnings, timeouts, average and maximum answer time, cost per
      match (`scripts/practice.py` `evaluate()` already reads a replay).
- [ ] Harden `scripts/check_image.py` until it covers the whole checklist of
      ARENA-CONTRACT 2.9.
- [ ] Metrics: every decision appends one JSON line (game, level used, time,
      tokens, why). This feeds tuning and the presentation. (Basic version
      exists in `bot/metrics.py`; add per-call tokens and cost.)
- [ ] Verify proxy behavior: model calls go straight to the gateway, anything
      else honors `HTTPS_PROXY` (urllib honors `NO_PROXY`; check in the arena).
- [x] Logo: a square `logo.png` (512 x 512) copied to `/logo.png` (uncomment
      the line in the Dockerfile).

luckydevs-2:

- [ ] Chat defense: untrusted wrapper (done, basic), injection detector
      (done, basic regexes), extraction of the opponent's claims as
      structured data passed into the decision.
- [ ] The decision prompt: robust JSON output, move and chat from one call,
      short and cache-friendly. Seen in practice: the model sometimes writes
      analysis before the JSON and hits `max_tokens` (then L1 is used).
- [ ] Opening chat must not leak our plan (seen: "I'm opening in the center").
      Seen again on arena-lite with the real model, also in move chat:
      "Corner A1 for me, blocking your column and building my diagonal
      toward C3". The chat must serve the move, not describe it.
- [ ] Guardrails: extract the forbidden subject and its synonyms in prep;
      outgoing filter on action and chat (also the `ready` chat); a safe
      rewrite when a line is blocked.
- [ ] Truth: answer only with a quote found in the document; otherwise the
      exact "unknown" answer from the rules.

luckydevs-3:

- [ ] Game profile and family classification from the rules text (basic
      LLM version exists in `bot/prep/classify.py`).
- [ ] Simulator generation: several candidates in parallel, fixed interface
      (`bot/interfaces.py`, SIMULATOR_FUNCTIONS). From the kit: the
      `sample_turn` is always seat 0; the simulator must read the deal from
      the observation (and `start.variant`), never from the prep rules text
      (RULES 30).
- [ ] Sandbox: generated code runs in a child process with time and memory
      limits and can never take down the bot.
- [ ] Verification: the simulator parses every `sample_turn`; its legal
      actions equal the sample's `legal_actions`; random self-play runs
      without errors; failures are sent back to the model for repair; the
      best candidate wins.
- [ ] Engine for Think ahead: iterative deepening alpha-beta with a time
      cap, MCTS when the branching is large, solved positions cached.
- [ ] Engine for Guessing: hypothesis set filtering and best-split choice.
- [ ] Wire L3 into the ladder, only when verification passed (the plumbing
      exists; set `spec.verified` and `spec.simulator_path` in prep).
- [x] A prepared specialist is found at `start` also when the rules text of
      the game differs from prep: lookup by game and variant label
      (`specialist.lookup_by_label`, done with the kit integration).
- [ ] Fallback action when `legal_actions` is null and the model is down:
      seen on arena-lite (Guess the Number) that a wrong example action
      loses the game in two answers. First fix done (`example_from_rules`
      now reads "Examples:"); still open: after a warning try the other
      quoted examples of the rules before the last-resort "pass".

## Part D: phase 2, T1 until the T2 freeze (Sat 23:59)

Milestones:

- [ ] 18:00 - 20:00: watch T1, write down every lost game and why. Dinner.
- [ ] T1's 4 games are released: `git pull` in `participant-kit/`, run them
      locally (`python scripts/practice.py --list`) and read the public
      replays of the best teams. Use the Practice page (10 runs per hour)
      against dvloper House and dvloper Master with the pushed image.
- [ ] 23:15: final `t2` push and digest check. Tag the commit. This is the
      most important push of the event.

luckydevs-1:

- [x] Test games for the families the practice arena does not cover, all
      subclasses of `arena.core.Game` in `testgames/`, all played by
      `tests/test_testgames.py`: `rps_twist.py` (simultaneous with a payoff
      twist), `kuhn_bet.py` (betting with hidden cards), `split_deal.py`
      (negotiation with private values), `doc_quiz.py` (answer from a
      document, "unknown" is worth more than an invention), `taboo.py`
      (a forbidden word in chat ends the game), `nim_deal.py` (the deal is
      embedded in the rules text, kit note K2).
- [x] Replay analysis tool: `scripts/replay_report.py` groups every game of
      ours by family and every loss by cause (timeout, illegal answer, crash,
      outplayed), from the replays and the metrics lines.
- [x] Cost and speed report: `scripts/replay_report.py` (tokens, cache hits,
      answer times, model spend per game, peak RSS and children per turn) and
      `scripts/batch_runner.py` (the same per match, against a baseline).
- [x] Memory under concurrency: `tests/test_memory_concurrency.py` starts real
      subprocesses against one /memory and checks atomic writes, the shared
      read-modify-write cache, harness-version cache invalidation and the
      install lock.

luckydevs-2:

- [ ] Chat offense per family: bluff lines consistent with the move,
      pressure, attempts to mislead the opponent's model.
- [ ] Deals: reservation value, concession schedule, bid shading in
      auctions; code decides accept or reject, the model writes the words.
- [ ] Opponent profiles in `/memory/opponents/`: how each team chats and
      plays, whether it reacts to our chat (each team meets us in the round
      robin and maybe again in T2 and T3).

luckydevs-3:

- [ ] Read the opponent: payoff matrix from the simulator, equilibrium by
      regret matching, opponent frequency model, capped exploitation.
- [ ] Bluff: sample hidden states consistent with the observations,
      expected value per action, mixed strategy so we are not readable.
- [ ] Live consistency check: every turn compare the simulator's legal
      actions with the arena's; on a mismatch drop to L2 for that game.
- [ ] A reliable fallback action when `legal_actions` is `null` (today: the
      example action from prep or from the rules text; see the TODO in
      `bot/ladder.py` `safe_action`).
- [ ] Specialist cache keyed by rules hash and harness version, verified
      again on load.

## Part E: phase 3, T2 until the T3 freeze (Sun 10:30) and the presentation

Milestones:

- [ ] 00:00 - 02:00: plan the night, sleep in shifts. The round robin runs
      by itself 02:00 - 06:30.
- [ ] From the round robin replays: group losses by family and fix them
      generically. T3 includes 9 games nobody has played, so generality
      decides.
- [ ] 09:00 - 10:30: watch T2. 10:15: final `t3` push if we are in the top
      8. If we are not, all remaining time goes to the presentation.
- [ ] 15:30: presentation. Each member introduces themselves in about 30
      seconds.

luckydevs-1:

- [x] Demo viewer: `scripts/demo_page.py` builds one self-contained page
      (no network, no script tag) from the replays, the metrics lines and the
      prep artifacts: the ladder with our own shares, every prepared variant,
      one match move by move with the reason and the chat line, and the
      results per family.
- [x] Numbers for the slides: `scripts/replay_report.py --notes NOTES.md`
      (verification pass rate, level per decision, cost, answer times,
      results, and the weakest family to fix next).
- [ ] Final freeze checks for `t3`.

luckydevs-2:

- [ ] Chat tuning from real replays: which lines worked, which injections
      we resisted, examples for the presentation.
- [ ] Slides: architecture, what failed, what we learned.

luckydevs-3:

- [ ] Run prep over every rules text we have and table the verification
      pass rate; fix the generic failures.
- [ ] Prep time budget tuning; behavior on a game of an unknown family.

## Part F: pitfalls

- stdout purity: only `bot/protocol.py` writes to stdout.
- `match_over` is not exit: wait for the next `start`.
- The key changes every match: `llm.set_key` on every `prep` and `start`.
- Thinking tokens count toward `max_tokens` and the clock; empty answers happen.
- The chat rule: always chat, always pass the opponent's chat to the decision.
- Only the gateway model, ever.
- Secrets never in Git or in the image.
- Pushing after the freeze changes nothing.
- Source must match the image: push only a clean, committed tree, tag the commit.
- Cached specialists: bump `HARNESS_VERSION` when prep output changes.
