# LuckyDevs - AI Harness Arena bot

Team **LuckyDevs** · EESTEC Olympics 14 · AI Harness Arena (challenge by dvloper.io)
Team members: Drăghici Daria-Ioana, Ioniță Sebastian, Popescu George Vlad

Image: `harbor.hackaton-2026.dvloper.io/luckydevs/bot` · tags `t1`, `t2`, `t3`
Source: this repository (read access given to the judges' accounts, rule 3.7).

---

## 1. The challenge

We do not build a bot for one game. We build a **mesh**: a Docker image that knows no
game in advance. When a game's rules arrive (in `prep`, or in `start` for a cold
container), the image has 10 minutes to turn itself into a specialist for that game,
then plays it against another team's specialist.

Fixed constraints (see the Candidate Rulebook):

| Topic | Rule |
|---|---|
| Games | 21 secret two-player text games in 7 families (bluff, deals, read the opponent, guessing, think ahead, truth, guardrails). Every game has variants; the match draws one. |
| Model | Only Claude Haiku 5.5 through the gateway. Any other hosted model disqualifies. Local models and any tool are allowed. |
| Clocks | prep 600 s, `ready` 30 s (120 s cold), 20 s per move. A missed clock loses the game. |
| Legality | One illegal move = warning, the second loses the game. A bad `ready` at start loses immediately. |
| Chat | Mandatory on every move (<= 200 chars). The opponent's chat must reach our decision logic. It is untrusted input. |
| Resources | 2 CPUs, 2 GB RAM, 256 processes per container. `/memory` is shared by all our containers. |
| Scoring | Tournaments 30, round robin 10, cost + robustness 5, presentation 30, harness design 25. |

The hard part is not any single game. It is being **correct on rules we have never
seen**: an illegal move costs a game, and a hardcoded assumption about a variant
costs one too.

---

## 2. Architecture

```
arena -> stdin (JSON lines) -> protocol loop -> specialist for the game
                                   |
              +--------------------+---------------------+
              |                    |                     |
         chat module         decision ladder        sandbox worker
   (offense/defense,        L1 safe fallback       (isolated simulator /
    untrusted opponent      L2 decide.py           solver processes)
    chat, <=200 chars)      L3 evaluator/search
                                   |
                            action_guard.py   <- single exit for every action
                                   |
                           stdout (protocol only), logs -> stderr
```

Main components (paths as in this repository):

- **`bot/action_guard.py`** - the only place that writes an action line. Every action
  is validated against the action grammar of the drawn variant. When `legal_actions`
  is present it is the authority and we send the exact string from the list. After a
  warning the rejected string is blacklisted and never resent.
- **Decision ladder** (`bot/engines/`, `bot/ladder.py`) - L1 is a guaranteed-valid
  fallback computed before anything heavy; higher levels (search, playouts, solvers)
  can only improve on it, never replace it with something unvalidated.
- **Sandbox worker** - heavy computation (simulators, search) runs in an isolated
  process with time and memory limits so a bug cannot crash the protocol loop.
- **Chat module** - move and chat come from the same decision. The opponent's chat is
  passed to the decision logic as marked, untrusted data and is never obeyed. Own
  private information is never put in chat. Speech rules from the rules text are
  enforced on every line, including the one sent with `ready`.
- **Memory** (`/memory/<game_id>/<rules_hash>/`) - tables, parsers, opponent notes;
  written atomically (temp file + `os.replace`), and a missing or corrupt file never
  crashes the bot.
- **Tooling** (`scripts/`) - `build.py` (labels the image with the git commit),
  `check_image.py` (ARENA-CONTRACT checklist, 13 checks), `push.py` (refuses to push a
  dirty tree or an image not built from HEAD).

---

## 3. What we did, in order

### T1 - first analysis of failures
Practice and T1 replays against `dvloper Master` showed three failure classes:

1. **Tactical blunders in Connect-N** (dropping a disc that lets the opponent win on
   the cell above, and losing a zugzwang/parity endgame on a 9x7 board).
   Flat Monte Carlo playouts did not punish 1-2 ply mistakes.
2. **Model-chosen moves went out unverified.** When `legal_actions` is `null`
   (more than 50 legal moves) the model's answer was sent as-is. This produced an
   illegal `guessSLIDE` in Wordle Duel (missing space) and a lost game.
3. **Moves ignoring known information**: Wordle guesses that repeated words and ignored
   green/yellow/gray feedback; Colonel Blotto allocations that put 40 troops on a
   worth-1 field.

### Between T1 and T2 (shipped in `t2`)
- Tactical engine for gravity games: immediate win, mandatory block, anti-feed filter
  (never play under an opponent's winning cell), threat-forcing search, exact endgame
  solving.
- `action_guard` as the single validated exit, with fallback on any exception.
- Deterministic Wordle solver: the candidate set is rebuilt from the observation each
  turn by keeping every word consistent with all feedback; the model only writes chat.
- Policy router and an allocation policy (Blotto-style games).
- A chat influence layer (teammate contribution) for non-chat-related games.
- Image verification (`check_image.py`, 13/13) before every push.

### T2 + round robin - what went wrong
The `t2` image finished near the bottom. The replays gave us the key lesson
(see [Lessons learned](#4-lessons-learned)): in Battleship, variant
**8x8, 4 ships, salvo** (one shot per ship afloat, e.g. `fire e1 f1 g1 h1`), our
policy was written for the 6x6 single-shot base variant, sent one shot, got a warning,
repeated the mistake and forfeited.

### After T2 - three parallel efforts
1. **Local tester with GUI** (`participant-kit-lab/`, built on `arena-lite`): runs two
   Docker images (frozen `t2` vs candidate `t3`) head to head, both seats, many seeds,
   reports W/D/L, warnings, timeouts, per-match winner. Also contains our own
   implementations of the released/new games for local testing.
2. **Policy suite for the games we knew** (`daria/t3-policy-suite`): Nim (nim-sum),
   Bulls and Cows (information-gain guessing), Pig (value iteration), Battleship
   (probability density), RPS (predictor ensemble), Prisoner's Dilemma (opponent model),
   partial Hex / Dots and Boxes / Mini Chess.
3. **Rules-driven redesign** (`daria/rules-driven-only`) - see next section.

---

## 4. Lessons learned

1. **Game-specific policies do not generalize.** A policy that hardcodes board size,
   shots per turn, dice count or target score acts confidently on a variant that
   differs, and one illegal move is a warning, two is a lost game. Of 21 games with
   several variants each, most are unknown to us in advance.
2. **A model move must never leave the process unverified.** Validation belongs at a
   single exit that every code path uses.
3. **Haiku is weak at exact deduction and calculation.** Code should pick the move
   (filtering candidates, search, probabilities); the model is best used to read rules,
   to write code, and to talk.
4. **Test on variants, not just on the base game.** Our unit tests passed on the base
   rules; the failure was a variant we had not tested.

---

## 5. Rules-driven design (direction of the final harness)

The bot should learn each game **only from what it receives at runtime**: the rules
text of the drawn variant, `sample_turn`, observations, history and chat. No
pre-written solver for a specific game.

1. **Policy applicability gate.** Any dedicated logic must extract its parameters from
   the rules text and abstain unless every parameter matches what it implements.
   Unknown variant -> generic path.
2. **Action grammar learner** (`bot/grammar.py`). Infer a validator from the rules text,
   `sample_turn.legal_actions`, and the move format that appears in history lines
   (the opponent's past moves show the real format). Supports multi-part moves
   (salvo, allocations that must sum to N, several items), uniqueness and
   already-used sets.
3. **Rules-to-simulator in prep.** Haiku writes `parse_observation`, `legal_actions`,
   `apply_action`, `is_terminal`, `returns` for the variant. The module runs in a
   sandboxed subprocess and is **accepted only if** it reproduces `sample_turn`
   parsing and `legal_actions` exactly, survives 200 random self-play games and
   terminates. Cached by rules hash in `/memory`.
4. **Generic decision engines on the accepted simulator** (they contain no game rules):
   alpha-beta with transposition table for perfect information, MCTS / determinized
   sampling for hidden information or large branching, expected-value / best-response
   against an opponent model for simultaneous and chance games, information-gain
   selection for deduction games.
5. **Safe fallback when no simulator is accepted.** Haiku proposes a move in a strict
   format; the grammar validates it; on failure we re-ask up to twice with the
   validator's error message; then a grammar-valid move chosen by code. Nothing
   unvalidated is ever sent.
6. **Same decision for move and chat**, with the opponent's chat treated as untrusted
   data.

---

## 6. Compliance with the rulebook

| Rule | How we comply |
|---|---|
| 5.1 / 14.7 stdout purity | stdout carries protocol JSON lines only (flushed); all logs go to stderr. Checked by `check_image.py`. |
| 5.11 / 7.2 keys | We always use the key from the latest `prep`/`start` message, never only the environment variable. |
| 6 chat | Every `start`/`turn` answer carries a chat line (<= 200 chars). The opponent's chat reaches the decision logic marked as untrusted. Private information is never sent. |
| 7.7 model rule | Only the gateway Haiku is called as a hosted model. No other hosted LLM, search/answer mode or hosted agent. |
| 7.11 no outside help | No game content is sent to any server people can read. Internet access (if used) goes through the arena proxy and is for non-game content only. |
| 8.1 legality | One exit (`action_guard`), grammar-validated, warning handler, blacklist of rejected strings. |
| 4.3 / 8.2 clocks | Fallback move computed before heavy work; deadline checks inside search; model calls have hard timeouts. |
| 5.14 memory | Per-game folder keyed by the rules hash; atomic writes; corrupt files are tolerated. |
| 7.8 | No human interaction with the bot, `/memory` or logs during the event. |

---

## 7. Build, test and ship

```bash
# unit tests
python3 -m unittest            # expect: RESULT: CLEAN

# build (labels the image with the git commit)
python3 scripts/build.py t3

# ARENA-CONTRACT checklist (13 checks, including a practice match with prep)
python3 scripts/check_image.py t3

# push (refuses if the tree is dirty or the image is not built from HEAD)
python3 scripts/push.py t3
docker buildx imagetools inspect harbor.hackaton-2026.dvloper.io/luckydevs/bot:t3
```

Local head-to-head tester: `participant-kit-lab/arena-lite` -> `python3 -m lab`
(see its `README_LAB.md`).

---

## 8. What is still weak / future work

- Generalization to the 13+ secret games depends on the quality of the simulator
  Haiku generates in prep; the acceptance test protects against wrong simulators but
  cannot make a weak one strong.
- Bluff, negotiation, truth and guardrails games have no dedicated engine; they rely on
  the grammar-validated LLM path plus opponent modeling.
- [add any known weak spots after the final tests]