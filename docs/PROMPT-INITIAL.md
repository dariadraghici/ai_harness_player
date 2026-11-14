# PROMPT-INITIAL: build the base of the luckydevs harness

You are working in the terminal, inside the Git repository of team `luckydevs`
(cloned from `https://git.hackaton-2026.dvloper.io/luckydevs/bot`). I am Sebi, the
team lead. This file is your full briefing. Read all of it before you touch anything.

Your job in this session: build the BASE of the project. That means a bot that
is already safe to submit, a clean module layout that three people can extend in
parallel without stepping on each other, the local tooling, and four documents
(RULES.md, READMEFIRST.md, GAMEPLAN.md, docs/ARENA-CONTRACT.md).

You are NOT expected to build the smart parts (simulator generation, search
engines, advanced chat). Those are assigned to teammates in GAMEPLAN.md. You
build the skeleton they plug into, with clear interfaces and stubs.

---------------------------------------------------------------------------

## 0. How to work in this session

1. We are in a live hackathon. The first submission deadline (T1 freeze) is
   today, Saturday 14 Nov 2026, at 18:00 local time. Prefer simple code that
   works over elaborate code. Target: the whole base finished and verified in
   about 60 minutes of work.
2. Work phase by phase, in the order of sections 3 to 9. At the end of each
   phase print a 3 to 6 line status (what you did, what you verified).
3. Ask me questions only when you are blocked or when this file tells you to
   ask. When you ask, batch all open questions in one message. For everything
   else, pick the sensible default, write it down in your final report, and
   keep going.
4. Always ask me before: `git push`, `docker push`, deleting or rewriting files
   that existed before you started, and any step that would spend more than
   about 0.05 USD of the dev model key.
5. Never invent facts about the arena. Section 2 is the authoritative contract.
   If the starter kit in the repo disagrees with section 2, the starter kit
   and its README win (they are newer); tell me about every difference you find.
6. Every file you write (code, docs, comments) must be in English and use only
   plain ASCII keyboard characters. No em dashes, no arrows, no curly quotes,
   no emoji, no special math symbols. Use "->", "x", "<=" and similar.
7. Do not edit the practice arena engine (the `arena-lite` folder or whatever
   it is called in the starter kit). Treat it as vendored, read-only code.

---------------------------------------------------------------------------

## 1. Context: what we are building and why

The hackathon is the "AI Harness Arena" by dvloper.io (EESTEC Olympics 14).
15 teams of 3. Every team submits one Docker image. The image is a "mesh": a
program that knows no game in advance. Before each tournament the arena starts
one container of our image per game, sends it the rules of a secret two-player
text game, and gives it 10 minutes alone ("prep") to turn itself into the
specialist for that game. Then specialists of different teams play each other,
20 seconds per move, and they also talk in a shared chat that is part of the
game. Every team uses the same hosted model (Claude Haiku 5.5 through the
organizers' gateway). The harness around the model is the only difference.

Games come from 7 families:

| Family | What it is |
|---|---|
| Bluff | hidden information, betting, bluffing, probabilities |
| Deals | negotiation, trust and betrayal, auctions with private values |
| Read the opponent | simultaneous moves, patterns, mixed strategies |
| Guessing | deduction from feedback |
| Think ahead | perfect-information board games, search, tactics |
| Truth | answer from a given document; an invented answer costs more than "unknown" |
| Guardrails | rules name a forbidden subject; never mention it, still answer the rest |

Scoring (100 points):

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

Our strategy (details go in GAMEPLAN.md, section 7.3 of this file):

- During prep the harness asks Haiku to WRITE a simulator of the game in
  Python, tests it against the sample turns the arena provides, and repairs it.
- During a match, CODE computes the move (search, probabilities, filtering),
  and one cheap Haiku call per turn reads the opponent's chat and returns the
  final move choice plus our chat line, as one decision.
- A fallback ladder guarantees that we always answer in time with a legal move:
  - L3: engine on a verified simulator
  - L2: Haiku picks a move, code validates it against the legal moves
  - L1: deterministic safe move (first legal move)
- Model spend during prep does not count for the cost score, only spend during
  matches. So expensive work belongs in prep.

In this session you build L1 and L2 for real, and the plumbing and stubs for L3.

---------------------------------------------------------------------------

## 2. The arena contract (authoritative summary of the rulebook)

In phase 4 you will copy this whole section, unchanged, into
`docs/ARENA-CONTRACT.md`, because my teammates and their coding agents do not
have the rulebook.

### 2.1 Container

- The arena runs our image with `docker run -i --rm`, with NO arguments. The
  image CMD or ENTRYPOINT must start the bot.
- Image must be built for `linux/amd64`.
- Limits per container: 2 CPUs, 2 GB RAM, 256 processes. Several of our
  specialists are alive at the same time, so an idle specialist must use
  almost no CPU and little memory.
- Network: the model gateway directly; everything else through the arena web
  proxy (HTTP and HTTPS only), which logs every host we contact.
- `/memory` is a directory that persists for the whole event and is shared by
  ALL our containers at the same time (concurrent writers).

### 2.2 Protocol: JSON lines

- The arena writes one JSON object per line to our stdin.
- We answer with one JSON object per line on stdout, UTF-8, flushed after
  every line.
- Logs go to stderr. Any other line on stdout while an answer is expected
  counts as a bad answer.

Life of a container:

```
prep -> ready -> (waits) -> [ start -> ready -> turn -> action ... end ] x every game it plays -> shutdown
```

| Message from arena | We answer | Clock |
|---|---|---|
| `prep` | `{"type": "ready"}` | 600 s |
| `start` | `{"type": "ready", "chat": "..."}` | 30 s (prepared specialist), 120 s (cold container) |
| `turn` | `{"type": "action", "action": "...", "chat": "..."}` | 20 s |
| `end` | nothing | - |
| `shutdown` | nothing; save to /memory and exit | 15 s |

A closed stdin must be treated exactly like `shutdown`. `match_over: true` in
an `end` message does NOT end the container; it keeps waiting for the next
`start`. Answer as soon as you are ready; early answers are fine.

`prep` fields:

- `game_id`, `game_name`
- `variants`: list; each has `label`, `rules` (complete rules text, including
  the exact action format) and an optional `sample_turn` (one complete real
  `turn` message from a sample game of that variant; do not depend on it
  always being present)
- `seconds` (600), `you` (our team id), `study_seconds` (30), `move_seconds` (20)
- `llm`: `{"base_url", "api_key", "model"}`, the key for this prep

`start` fields:

- `match_id`, `game_index`, `best_of` (3 in tournaments, 4 in the round robin)
- `game_id`, `game_name`, `variant` (its `label` and settings), `rules`
  (complete rules text of the drawn variant)
- `seat` (0 or 1), `you`, `opponent` (team ids)
- `match_score`: `{"you", "opponent"}`
- `study_seconds`, `move_seconds`
- `chat`: the whole match chat so far
- `llm`: the model key for this match; switch to it immediately

`turn` fields:

- `game_index`, `turn` (decision counter from 1, shared by both players)
- `observation`: text; everything we may know right now (private info and
  public history)
- `legal_actions`: the complete list of legal moves when there are at most 50,
  otherwise `null` (then the rules text defines the action format)
- `seconds` (20)
- `chat`: the whole match chat so far
- `warning`: present only when our previous answer was rejected:
  `{"count", "max", "reason", "rejected"}`

`end` fields: `game_index`, `result` (`win`, `loss`, `draw`), `reason`,
`scores` (`{"you", "opponent"}`), `match_score`, `final_observation`,
`match_over`, `chat`.

Chat entries (in every `chat` list, oldest first): `from` (`you` or
`opponent`), `game`, `game_name`, `phase` (`study` or `play`), `turn`, `kind`
(`chat`, or `move` in games where talking is the move), `text`.

Cold container: if a specialist is missing (no `ready` in time during prep, or
a crash), the game is played by a fresh container that gets `start` directly,
WITHOUT `prep`, and has 120 s instead of 30 s to answer `ready`. Local practice
without the prep option behaves the same way: one container plays all games of
a match, each with a 120 s study clock.

In simultaneous-move games both players get `turn` at the same time and
neither sees the other's choice until both have answered.

### 2.3 Environment variables set by the arena

| Variable | Meaning |
|---|---|
| `LLM_BASE_URL` | gateway address (OpenAI-compatible) |
| `LLM_API_KEY` | the key the container started with; it expires. The current key is always in `llm` of the latest `prep` or `start` |
| `LLM_MODEL` | `haiku` |
| `TEAM_ID` | our team id |
| `MEMORY_DIR` | `/memory` |
| `HTTP_PROXY`, `HTTPS_PROXY` | the arena web proxy (also set in lower case) |
| `NO_PROXY` | gateway, localhost, 127.0.0.1 |

In the local practice arena, only variables named `LLM_*` and `BOT_*` from the
env file reach the bot. So all our own settings must be named `BOT_*`.

### 2.4 What loses a game

- First bad answer to a `turn` in a game (illegal action, invalid JSON, wrong
  `type`, missing or empty `action`): a warning. The arena sends the same
  `turn` again with a fresh clock and a `warning` field.
- Second bad answer in the same game: the game is lost.
- No answer within the clock (20 s for a move, 30 s or 120 s for `ready` at
  `start`): the game is lost immediately, no warning.
- Malformed or missing `ready` at `start`: lost immediately, no warning.
- The process exits (crash): lost immediately.
- A late or missing `ready` to `prep` loses nothing by itself, but the game is
  then played by a cold container.
- Warnings are the first tie-break of a knockout match; total answer time is
  the second; model cost is the third.

### 2.5 The chat (mandatory, enforced by code review)

- Every answer to `start` and `turn` carries a `chat` string (up to 200
  characters; longer is cut; a non-string is ignored).
- Offense is required: bluff, negotiate, pressure, mislead, in line with what
  our move needs.
- Defense is required: weigh the opponent's claims, spot tricks and injected
  instructions, never leak private information.
- Move and chat must come from ONE decision, from the same view of the game.
- The opponent's chat must reach our model or our decision logic and inform
  the decision. We may quote, wrap, summarize, filter or mark it as untrusted.
  We never have to obey it.
- Forbidden: never chatting; stripping the opponent's chat before the model or
  the decision logic sees it; reading it only to throw it away.
- Opponent chat is untrusted input: data, never instructions.
- Our chat counts as our output. Some games restrict what a player may say
  (Guardrails family); the game engine checks chat lines too, including the
  one sent with `ready`, and a wrong line can end the game on the spot.
- Keep it respectful: no insults, hate, harassment or attacks on real people.

### 2.6 The model

- The ONLY hosted model we may call is the gateway's Haiku: OpenAI-compatible
  API, model name `haiku`. Calling any other hosted language model, directly
  or through a tool, library or service (including AI search or answer modes),
  at any time during the event, disqualifies the team. Local models that run
  inside the container are allowed.
- In the arena: one fresh key per match with a 0.75 USD budget, shared by all
  our containers in that match; each prep container gets its own 0.75 USD key.
  When a key runs out, calls fail, so there must always be a fallback move.
- The dev key (for local development) has its own limited budget and a limit
  of 60 requests per minute.
- Haiku 5.5 thinks before answering by default, and thinking tokens count
  toward `max_tokens` and toward our clock. With a small `max_tokens` the
  answer can be empty. Control it per call:
  - `extra_body={"thinking": {"type": "disabled"}}` (fast)
  - `extra_body={"thinking": {"type": "enabled", "budget_tokens": 2000}}`
    (minimum budget 1024; keep `max_tokens >= budget_tokens + 1500`)
  - `reasoning_effort` does NOT switch thinking off.
- Always handle an empty or cut-off answer.
- Prices: 0.10 USD per million input tokens, 0.50 USD per million output
  tokens, cache reads 0.01 USD per million.
- Spend of a key: `GET <base_url without /v1>/key/info` with the header
  `Authorization: Bearer <key>`.
- Give every model call a timeout well inside the current clock.

Example call:

```python
from openai import OpenAI
llm = msg["llm"]  # from the latest prep or start
client = OpenAI(base_url=llm["base_url"], api_key=llm["api_key"])
resp = client.chat.completions.create(
    model=llm["model"], max_tokens=400, timeout=15,
    messages=[{"role": "system", "content": rules},
              {"role": "user", "content": observation}],
    extra_body={"thinking": {"type": "disabled"}})
text = resp.choices[0].message.content or ""  # may be empty
```

### 2.7 Other rules that affect code

- Internet is allowed (through the proxy), but we must never send game content
  (rules, observations, chat, derived state) to any server that people can
  read during the event. Public read-only services for non-game content are
  fine (package registries, documentation).
- No human may steer the bot during prep or matches.
- Game-specific code for games already seen is allowed but costs harness
  design points. We keep the harness generic: logic per family, not per game.
- The judges must have read access to the source of every submitted image,
  with a Git tag or commit per freeze (`t1`, `t2`, `t3`). Source that does not
  match the frozen image is an offence.
- Optional logo: `/logo.png` in the image (at most 1 MB, at most 2048 px per
  side, 512 x 512 is plenty).

### 2.8 Schedule (local time, Europe/Bucharest)

| When | What |
|---|---|
| Sat 14 Nov, 12:00 | hacking starts |
| Sat 18:00 | T1 freeze (tag `t1`), then prep, then T1 live |
| Sat 23:59 | T2 freeze (tag `t2`); this image also plays the round robin |
| Sun 02:00 - 06:30 | round robin, 105 matches, automatic |
| Sun 09:00 - 10:30 | T2 matches, live |
| Sun 10:30 | T3 freeze (tag `t3`), only for the top 8 of the round robin |
| Sun 13:00 - 15:00 | T3 on stage |
| Sun 15:30 | presentations |

T1 pool: 4 games. T2 pool: 8 other games. Round robin: those 12. T3: drawn
from all 21 games, including games nobody has seen. After T1 and after T2 the
games of that tournament are released for local practice.

### 2.9 Pre-push checklist (rulebook 14.7)

- `docker run -i --rm <image>` starts and waits for input, without exiting or
  printing anything.
- A full practice match with prep, played by the IMAGE: `ready` in time, no
  warning, no timeout.
- After `match_over` it waits for the next `start`; only `shutdown` or a
  closed stdin ends it.
- It uses the key from the latest `prep` or `start`, not only the one in its
  environment.
- It chats, and the opponent's chat reaches its decisions.
- With the model slow or failing (key out of budget) it still answers in time
  with a safe fallback move.
- stdout carries only protocol lines; logs go to stderr.
- Anything fetched from the internet goes through `HTTPS_PROXY`, and no game
  content leaves for a server people can read.
- The image is built for `linux/amd64` and the registry serves the digest
  just pushed.
- The matching source is in the repository the judges can read.

---------------------------------------------------------------------------

## 3. Phase 0: look before you build

1. Run `git status`, `git branch -a`, `git log --oneline -10` and list the
   repository tree (2 levels). Tell me which branch you are on. The base must
   be built on `main`.
2. Find the starter kit. The rulebook says it contains a sample bot
   (`starter/main.py` plus its Dockerfile) and the practice arena
   (`arena-lite/`, run with `python -m arena ...` from inside that folder).
   Read the sample bot, its Dockerfile, every README, and the arena command
   line help (`python -m arena --help`, `python -m arena practice --help`,
   `python -m arena practice --list`).
3. If the starter kit is NOT in the repository, stop and ask me where it is.
   Do not write a fake arena.
4. Check the tools: `python --version` (need 3.10 or newer), `docker version`,
   `docker buildx version`. Detect the operating system. If something is
   missing, tell me and continue with what can be done.
5. Write down every difference between the starter kit and section 2 of this
   file. You will report them and apply them to `docs/ARENA-CONTRACT.md`.

---------------------------------------------------------------------------

## 4. Phase 1: secrets, keys and access

Non-secret values (safe to commit):

| Thing | Value |
|---|---|
| Team | `luckydevs` |
| Registry (Harbor) | `harbor.hackaton-2026.dvloper.io` |
| Image | `harbor.hackaton-2026.dvloper.io/luckydevs/bot:<tag>` with tags `t1`, `t2`, `t3` |
| Harbor robot user | `robot$luckydevs+bot` (contains a `$`: always single-quote it in a shell) |
| Git (Gitea) | `https://git.hackaton-2026.dvloper.io/luckydevs/bot` |
| Model gateway | `https://llm.hackaton-2026.dvloper.io/v1` |
| Model name | `haiku` |
| Arena dashboard | `https://arena.hackaton-2026.dvloper.io/` |
| Practice page | `https://arena.hackaton-2026.dvloper.io/#/practice` (needs a practice token) |

Secret values (the Harbor robot secret, the model dev key, the practice
token): I will put a file named `secrets.local.env` in the repository root.
It has plain `KEY=VALUE` lines, no quotes:

```
LLM_BASE_URL=...
LLM_API_KEY=...
LLM_MODEL=haiku
HARBOR_REGISTRY=...
HARBOR_USER=...
HARBOR_SECRET=...
IMAGE=...
PRACTICE_URL=...
PRACTICE_TOKEN=...
```

Steps, in this exact order:

1. BEFORE anything else, create `.gitignore` and `.dockerignore` so that
   secrets can never be committed or baked into the image. Both must exclude:
   `secrets.local.env`, `dev.env`, `*.env` (but keep `*.env.example`),
   `results/`, local memory folders created by the practice arena,
   `__pycache__/`, `.venv/`, `*.pyc`, editor folders. `.dockerignore` must
   also exclude `.git`, `tests/`, `docs/`, `results/` and the practice arena
   folder. This matters: the judges can read the repository and the
   organizers pull the image.
2. If `secrets.local.env` does not exist, stop and ask me for it. Never print
   secret values in your output; print only the key names you found.
3. Never `source` that file in a shell (the `$` in the robot user would be
   expanded). Parse it in Python, line by line, splitting on the first `=`.
4. Create `dev.env` in the location the practice arena expects (the rulebook
   example puts it next to the `arena-lite` folder and passes it with
   `--env-file ../dev.env`). It contains only `LLM_BASE_URL`, `LLM_API_KEY`,
   `LLM_MODEL`, plus any `BOT_*` settings we need locally.
5. Create `dev.env.example` and `secrets.local.env.example` with the same keys
   and empty values. These are committed, so teammates know what to fill in.
6. Verify the model key with ONE tiny call (thinking disabled, `max_tokens`
   around 20, prompt "Say OK"). Then call the key info endpoint
   (`https://llm.hackaton-2026.dvloper.io/key/info`, Bearer header) and
   report the budget and the spend so far. Do not print the key.
7. Log in to Harbor without putting the secret on the command line:
   pass it through `--password-stdin` from Python (`subprocess.run` with
   `input=`), user `robot$luckydevs+bot`. Report success or the exact error.
8. The practice token: check whether the starter kit or its docs mention how
   the practice page or token is used. If they do, wire it in. If they do not,
   leave it stored in `secrets.local.env`, mention it in READMEFIRST.md, and
   tell me in the final report that I should check the practice page myself.
9. Run `git status` and confirm that no secret file is tracked or staged.

---------------------------------------------------------------------------

## 5. Phase 2: the project skeleton

### 5.1 Layout

Create this layout. If the starter kit forces a different location for
something, adapt and tell me. Use Python (same major version as the starter
kit's Dockerfile). Dependencies: only what the starter kit's Dockerfile
installs (copy its exact `openai` version pin; do not guess one). Adding a new
dependency later needs my approval.

```
bot/                     the harness (a Python package; this is what runs in the image)
  __init__.py
  main.py                entrypoint: the protocol loop, tiny and bulletproof
  protocol.py            the ONLY module that writes to stdout; message parsing
  clock.py               deadlines and the watchdog that guarantees an answer in time
  config.py              all constants and BOT_* settings in one place
  log.py                 logging to stderr
  interfaces.py          shared data classes and interfaces (the contracts between modules)
  llm.py                 gateway client: key switching, thinking control, timeouts, spend tracking, fake mode
  memory.py              the /memory store: atomic writes, per-game folders, rules hash
  specialist.py          one object per game: what prep produced, loaded per variant
  ladder.py              the fallback ladder L3 -> L2 -> L1 that produces every decision
  metrics.py             one JSON line per decision (level used, time, tokens), for tuning and the demo
  opponents.py           opponent profiles in /memory/opponents (stub)
  prep/
    __init__.py
    pipeline.py          orchestrates prep under a time budget
    classify.py          game family and game profile (stub with a simple LLM version)
    codegen.py           simulator generation by the model (stub)
    verify.py            tests of a generated simulator against sample turns (stub)
    sandbox.py           runs generated code in a child process with a timeout (stub)
  engines/
    __init__.py
    base.py              Engine interface and registry: family -> engine
    search.py            Think ahead: alpha-beta / MCTS (stub)
    guess.py             Guessing: hypothesis filtering (stub)
    simultaneous.py      Read the opponent: mixed strategy + opponent model (stub)
    bluff.py             Bluff: Monte Carlo over hidden states (stub)
    deals.py             Deals: negotiation and auction policy (stub)
    truth.py             Truth: answer only with support from the document (stub)
    guardrails.py        Guardrails: forbidden-subject filter on everything we output (stub)
  chat/
    __init__.py
    defense.py           marks opponent chat as untrusted, detects injections (basic version now)
    offense.py           chat tactics per family (stub)
    decide.py            the single model call that returns move + chat as one decision
  prompts/               prompt templates as plain text files
tests/                   stdlib unittest; includes a fake LLM and a scripted arena session
scripts/                 cross-platform Python scripts (no bash): build, push, checks, practice
docs/
  ARENA-CONTRACT.md      section 2 of this file
  PROMPT-INITIAL.md      this file (move it here at the end)
Dockerfile
.gitignore
.dockerignore
dev.env.example
secrets.local.env.example
RULES.md
READMEFIRST.md
GAMEPLAN.md
AGENTS.md                3 lines: tells any coding agent to read RULES.md and READMEFIRST.md first
CLAUDE.md                same 3 lines
```

### 5.2 Behavior that must be REAL in the base (not stubs)

These are the parts that keep us from ever losing a game for a stupid reason.
Implement them fully and test them.

Protocol loop (`main.py`, `protocol.py`):

1. At startup, keep a private handle to the real stdout for protocol lines,
   then point `sys.stdout` at stderr. That way a stray `print` in our code, in
   a library or in generated code can never corrupt the protocol.
2. Read stdin line by line. An empty read (stdin closed) is a shutdown.
3. An input line that is not valid JSON, or has an unknown `type`: log it and
   continue. Never crash on input.
4. Exactly one answer per `prep`, `start` and `turn`. Use a lock and an
   "already answered" flag per request, so a slow worker that finishes after
   the watchdog already answered cannot send a second line.
5. Every answer is one line of JSON, written and flushed in one call.
6. The top level catches every exception, logs the traceback to stderr and
   keeps the loop alive. The process exits only on `shutdown` or closed stdin.
7. `end` with `match_over: true` does not exit. The bot waits for the next
   `start`. While it waits it must not spin the CPU.
8. On `shutdown` or closed stdin: save what is worth saving to memory and exit
   within 10 seconds.

Clock and watchdog (`clock.py`), with these defaults in `config.py`:

| Constant | Value | Meaning |
|---|---|---|
| `PREP_HARD_S` | 510 | latest moment we answer `ready` to `prep`, whatever state we are in |
| `START_WARM_HARD_S` | 18 | latest `ready` when the specialist is prepared (clock is 30 s) |
| `START_COLD_HARD_S` | 95 | latest `ready` for a cold start (clock is 120 s) |
| `TURN_SOFT_S` | 6 | target time for a move (answer time is a tie-break) |
| `TURN_HARD_S` | 13 | the watchdog sends the best move found so far (clock is 20 s) |
| `LLM_MATCH_TIMEOUT_S` | 8 | timeout of a model call during a match |

Derive the hard deadlines from the `seconds`, `study_seconds` and
`move_seconds` fields of the messages when they differ from the defaults
(keep the same proportion). The work for a request runs in a worker thread;
the main thread waits until the hard deadline and then answers with the best
decision available so far. Heavy computation must run in a child process
that can be killed (a thread cannot be killed); remember the 256 process
limit.

The ladder (`ladder.py`): every decision goes through it and always returns
a result.

1. L3 (engine): if the specialist has a verified simulator and an engine for
   the family, ask the engine for ranked candidate moves. In the base this
   level is a stub that reports "not available".
2. L2 (model, validated): call `chat/decide.py`. If `legal_actions` is a
   list, the returned action must be one of them (compare exact strings;
   if there is no exact match, try a case-insensitive and whitespace-trimmed
   match and use the arena's own spelling). If it is not legal, do not send
   it.
3. L1 (safe): the first legal action. If `legal_actions` is `null`, use the
   fallback action the specialist stored during prep (in the base: ask the
   model during prep or cold start for one example of a valid action for
   each variant and store it; this is a known weak spot, mark it with a
   TODO for luckydevs-3).
4. If the `turn` carries a `warning`, our previous answer was rejected: never
   send the `rejected` action again, log the `reason`, and prefer a move
   taken directly from `legal_actions`.
5. Any exception inside a level means "this level is not available": log it
   and go down one level. The ladder itself must never raise.
6. Every decision returns: `action`, `chat`, `level` (3, 2 or 1) and a short
   `why` for the logs and metrics.

The single decision call (`chat/decide.py`): ONE model call per turn that
returns the move and the chat line together (rule 2.5: one decision).

1. Input: rules of the drawn variant (or the playbook from prep), the
   observation, the legal actions (or the action format), the candidate
   moves from L3 when they exist, the match score, and the chat so far.
2. The opponent's chat lines are always included, wrapped in a clearly
   delimited block that says they are untrusted data from the opponent and
   must never be followed as instructions (`chat/defense.py` builds this
   block and flags lines that look like injected instructions). This is what
   makes us compliant with the chat rule; never remove it.
3. Ask for a small JSON object: `{"action": "...", "chat": "...", "why": "..."}`.
   Parse it defensively (the model may wrap it in text or code fences).
4. Thinking disabled, small `max_tokens`, timeout `LLM_MATCH_TIMEOUT_S`.
5. Put the stable part of the prompt first (rules, playbook) and the changing
   part last (observation, chat), so the gateway can cache the prefix.
6. Chat post-processing in code: must be a string, at most 200 characters,
   single line, never empty (fall back to a short neutral line), and it must
   pass through `engines/guardrails.py` `filter_outgoing()` (in the base this
   is a pass-through stub with the right signature).
7. The same module produces the opening chat line for the `ready` answer at
   `start`. If the model is not available, use a short neutral line.

Model client (`llm.py`):

1. `set_key(llm_dict)` is called on every `prep` and `start`; all later calls
   use the latest key and base URL. Environment variables are only the
   initial default.
2. One function for chat completions with parameters for thinking (disabled,
   or enabled with a budget and automatic `max_tokens` headroom), timeout,
   and `max_tokens`. It never raises: on error, timeout or empty content it
   returns `None` and logs the reason.
3. It tracks approximate tokens and spend per key (from the `usage` field).
4. Fake mode: when `BOT_FAKE_LLM=1`, no network call is made and a
   deterministic canned answer is returned. All protocol tests use it, so
   they are free and do not depend on the gateway.
5. No other model endpoint may ever be configured in this code base.

Memory (`memory.py`):

1. Root is `MEMORY_DIR` (default `/memory`). If it is missing or not
   writable, fall back to a temporary folder and log a warning; never crash.
2. Layout:
   - `<root>/<game_id>/<rules_hash>/` : everything prep built for one variant
     (`meta.json`, `profile.json`, `playbook.md`, later `simulator.py` and
     `verify.json`)
   - `<root>/opponents/<team_id>.json` : opponent profiles
   - `<root>/metrics/` : one JSONL file per container
3. `rules_hash` is a short SHA-256 of the normalized rules text.
4. `meta.json` stores a `HARNESS_VERSION` constant from `config.py`. A cached
   artifact made by a different harness version is ignored.
5. All writes are atomic: write to a temporary file with a name unique to
   this process, then `os.replace`. Several containers write at once.

Prep (`prep/pipeline.py`), base version:

1. For each variant: compute the rules hash; if a valid cached specialist
   exists in memory, load it and skip the work.
2. Otherwise make one model call per variant (thinking enabled with a
   moderate budget; prep spend does not count for the cost score) that
   returns: the game family (one of the 7, or `unknown`), a short playbook
   (how to play well, what to say in chat, what never to say), the exact
   action format, and one example of a valid action. Store it.
3. If `sample_turn` is present, store it next to the playbook; later it is
   the test case for generated simulators.
4. Run variants concurrently, a few at a time.
5. Answer `ready` as soon as everything is done, and at `PREP_HARD_S` at the
   latest, whatever is finished by then.
6. Leave clearly marked extension points where luckydevs-3 will add
   simulator generation, verification and engine selection.

Start (`main.py` + `specialist.py`):

1. Switch the model key first.
2. Find the specialist for `game_id` and the drawn variant: in this process,
   else in memory (another container of ours may have prepared it), else do
   a quick cold prep of that single variant inside the study clock.
3. Answer `ready` with an opening chat line within the hard deadline.

### 5.3 Stubs

Everything marked "(stub)" in the layout gets:

1. A module docstring that says what the module is for, who owns it
   (luckydevs-1, -2 or -3, see section 7.3) and what "done" means.
2. The function or class signatures that other modules already call, defined
   in or imported from `bot/interfaces.py`.
3. A body that raises `NotImplementedError` or returns "not available". The
   ladder already treats that as "go down one level", so the bot works today
   and gets stronger as stubs are filled in.

`bot/interfaces.py` must define at least:

- `GameProfile`: family, perfect or hidden information, simultaneous or
  alternating moves, speech restrictions, action format, example action.
- `Candidate`: an action with a value and an optional note.
- `Decision`: action, chat, level, why.
- `Engine`: `candidates(specialist, turn_msg, deadline) -> list[Candidate]`.
- The contract of a generated simulator module (as a documented draft that
  luckydevs-3 may refine): `parse_observation(observation, variant, seat)`,
  `legal_actions(state)`, `apply_action(state, action)`, `is_terminal(state)`,
  `returns(state)`, `current_player(state)`.
- `ChatContext`: the chat so far, split into ours and theirs, with the
  injection flags from `chat/defense.py`.

### 5.4 Dockerfile

Start from the starter kit's Dockerfile. It must: copy the `bot` package,
install the same dependencies, and start with unbuffered output, for example
`CMD ["python", "-u", "-m", "bot.main"]`. Keep the image small. If a
`logo.png` exists in the repository root, copy it to `/logo.png`; if it does
not exist, leave a commented line and list "logo" as a to-do in GAMEPLAN.md.

---------------------------------------------------------------------------

## 6. Phase 3: tests and scripts

Tests (`tests/`, stdlib `unittest`, all with `BOT_FAKE_LLM=1`):

1. A scripted arena session that starts the bot as a child process and sends:
   `prep`, `start`, several `turn` messages, `end` (not `match_over`), a
   second `start` and game, `end` with `match_over`, a third `start` for a new
   match, `shutdown`. It checks that every answer is valid, arrives in time,
   that nothing else appears on stdout and that the process exits cleanly.
2. Cold start: `start` without any `prep`.
3. Garbage input lines and unknown message types do not kill the bot.
4. A `turn` with a `warning` does not repeat the rejected action.
5. A `turn` with `legal_actions: null` still produces an action.
6. A worker that is too slow: the watchdog answers before the hard deadline
   (use a short test clock).
7. Model failure: with the fake LLM set to fail, every answer is still valid.
8. Closed stdin ends the process within the time limit.
9. Memory: atomic write, load after restart, wrong harness version ignored.

Scripts (`scripts/`, Python, standard library only, must work on Windows,
macOS and Linux; adapt paths and commands to the real starter kit):

| Script | What it does |
|---|---|
| `scripts/build.py <tag>` | `docker build --platform linux/amd64` of the image with that tag |
| `scripts/push.py <tag>` | refuses to run when the Git working tree is dirty (source must match the image); logs in with `--password-stdin`; pushes; runs `docker buildx imagetools inspect`; prints the digest and the Git commit; reminds me to tag that commit |
| `scripts/check_image.py <tag>` | automates the checklist of section 2.9 as far as possible: silent start, scripted session against the IMAGE, a practice match with prep against the random bot, stdout purity |
| `scripts/practice.py` | thin wrapper around the practice arena with our env file: choose game, opponent, prep on or off, local command or image |
| `scripts/keyinfo.py` | prints budget and spend of the dev key, never the key itself |

---------------------------------------------------------------------------

## 7. Phase 4: the documents

### 7.1 RULES.md (rules for every human and every coding agent on the team)

My two teammates work with other CLI coding agents. RULES.md is the first
thing their agents read, so write it as direct, numbered, unambiguous rules.
It must contain all of the following.

Git workflow:

1. Each person works only on their own branches. Branch name format:
   `<name>-<feature>`, lowercase, words separated by hyphens. `<name>` is the
   person's account name (`luckydevs-1`, `luckydevs-2`, `luckydevs-3`) or
   first name; `<feature>` is a short description. Example:
   `luckydevs-2-chat-defense`.
2. Create every branch from the latest `origin/main`
   (`git fetch origin` then `git switch -c <name>-<feature> origin/main`).
3. Never commit to `main`. Never push to `main`. Never merge anything into
   `main`. Never open or complete a merge yourself.
4. Never check out, commit to, push to, rebase or delete a branch that
   belongs to someone else.
5. Merges into `main` and all conflict resolution are done manually by Sebi
   and by nobody else, human or agent.
6. Do not run `git merge`, `git rebase`, `git cherry-pick`, `git reset --hard`
   on shared history, or any force push. If your branch needs something that
   landed on `main` after you branched, stop and tell your human; they talk
   to Sebi. The default answer is: finish the feature, hand it over, start
   the next feature on a new branch from the new `origin/main`.
7. Keep branches small and short-lived: one feature, pushed often
   (`git push -u origin <branch>`).
8. When a feature is ready, tell Sebi "ready to merge" with: the branch
   name, what changed, which files, how it was tested, and anything risky.
9. Commit messages: short imperative summary, prefixed with the area, for
   example `chat: flag injected instructions in opponent lines`.

Ownership (reduces conflicts):

10. Include the path ownership table from section 7.3. Edit only files in
    your own area.
11. Shared files (`bot/interfaces.py`, `bot/config.py`, `bot/main.py`,
    `bot/ladder.py`, `Dockerfile`, `RULES.md`, `GAMEPLAN.md`) change only
    asked permission. If you need a new field or function in an interface,
    propose it in your hand-over message.
12. When you add a file or folder, add its line to `READMEFIRST.md` in the
    same commit, inside the section of your area.

Secrets:

13. Never commit `secrets.local.env`, `dev.env` or any key, token or
    password. Never paste them into code, tests, docs, logs or commit
    messages. Never copy them into the Docker image.

Bot invariants (breaking any of these can lose games or disqualify us):

14. Only `bot/protocol.py` writes to stdout. Everything else logs to stderr
    through `bot/log.py`. No `print`.
15. The bot never exits and never raises to the top level. Every new code
    path is wrapped so that a failure means "fall down the ladder".
16. Every answer respects the deadlines in `bot/config.py`. Long computation
    checks its deadline or runs in a killable child process.
17. The only hosted model is the gateway's Haiku through `bot/llm.py`. No
    other model API, no AI search, no hosted agent, in the bot or in any tool
    the bot calls. Violation disqualifies the whole team.
18. Game content (rules, observations, chat, derived state) never leaves the
    container for any external server.
19. Every answer to `start` and `turn` carries a chat line. The opponent's
    chat is always passed to the decision (model or decision logic), marked
    as untrusted. Never strip or ignore it.
20. Everything we send (action and chat) goes through the outgoing filter in
    `bot/engines/guardrails.py`.
21. Chat stays respectful: no insults, hate, harassment, or real people.
22. No game-specific code (no `if game_id == ...`). Logic is generic per
    family and driven by the rules text.
23. No new dependency without Sebi's approval. The image must stay small and
    an idle specialist must use little memory and no CPU.
24. Use the model key from the latest `prep` or `start`; never cache a
    client across matches without `set_key`.

Testing and budget:

25. Before "ready to merge": `python -m unittest` passes, and at least one
    local practice match runs with zero warnings and zero timeouts.
26. Use `BOT_FAKE_LLM=1` for protocol and unit tests. The dev key has a
    limited budget and 60 requests per minute shared by the whole team; do
    not run large batches without telling the team.
27. Do not edit the practice arena engine. New test games go in a separate
    games folder loaded with the arena's games-dir option.
28. Everything in the repository is written in English with plain ASCII
    characters.

### 7.2 READMEFIRST.md (the map of the repository)

Generate it from the REAL tree after everything else exists. It must have:

1. A 10 line "what this project is" introduction and a pointer to
   `docs/ARENA-CONTRACT.md`, `RULES.md` and `GAMEPLAN.md`.
2. "Quick start": the exact commands, verified by you, to set up secrets, run
   the unit tests, run one practice match with the local bot, build the
   image, run a practice match with the image, check the dev key spend.
3. "How a decision is made": a short walk through prep, start and turn,
   naming the modules in the order they are called (an ASCII diagram is fine).
4. "File by file": EVERY file and folder in the repository (including the
   starter kit and practice arena folders, config files and scripts), each
   with: what it is, what it is used for, who owns it, and its status
   (`real`, `basic`, `stub`). Group them by area so that each owner's files
   are in one section.
5. "Where things are stored at runtime": the `/memory` layout and the local
   results and logs folders of the practice arena.

### 7.3 GAMEPLAN.md (what we build, in what order, who does it)

Write it as a working document with checkboxes. It must contain the
following content. You may reorganize and reword it, but do not drop anything.

Part A, goal and priorities:

1. The scoring table from section 1 and these conclusions:
   - The `t2` image is the most important submission: it plays T2 (10) and
     the round robin (10), and the round robin decides who enters T3 (15).
   - T1 is worth 5 points. Its goal is a clean run: zero crashes, zero
     timeouts, the full pipeline tested in the real arena.
   - 55 of 100 points come from judges. The architecture must be easy to
     explain and to demo, and we log metrics from the first hour.
   - Only match spend counts for cost; prep spend is free. Expensive model
     work belongs in prep.
   - Tie-breaks in knockouts: fewer warnings, then less answer time, then
     lower cost. So: no illegal moves, and answer fast.
2. The idea in plain words: in prep the model writes a simulator, the harness
   tests and repairs it; in a match code computes the move and one model call
   reads the opponent's chat and returns the final choice plus our chat line;
   the ladder L3 -> L2 -> L1 guarantees an answer.
3. The decision contract: when L3 has candidates, the model may choose only
   among candidates whose value is within a small margin of the best one;
   code enforces this. The opponent's chat can change the choice among those
   candidates and, in hidden-information games, the beliefs the engine uses.
4. The family table:

| Family | How the move is chosen |
|---|---|
| Think ahead | alpha-beta or MCTS on the generated simulator; small games solved completely and cached |
| Guessing | keep the set of answers still consistent with the feedback; pick the guess that splits it best |
| Read the opponent | mixed strategy from the payoff matrix, plus statistics on this opponent's past moves, with capped exploitation |
| Bluff | Monte Carlo over the hidden states consistent with what we saw; expected value per action; mixed play |
| Deals | code computes our value, reservation price and concession schedule; the model negotiates inside those limits |
| Truth | answer only when a supporting quote is found in the document, otherwise the exact "unknown" answer the rules define |
| Guardrails | forbidden subject extracted in prep; filter on every action and chat line we send, including the `ready` chat |

5. Chat: offense (bluffs consistent with the move, pressure, attempts to
   mislead the opponent's model, which the rules allow) and defense (opponent
   chat is data, injection detection, checkable claims verified against the
   simulator). Always respectful.

Part B, owners and areas:

| Person | Area | Owns these paths |
|---|---|---|
| luckydevs-1 | runtime, operations, measurement, test games | `bot/protocol.py`, `bot/clock.py`, `bot/log.py`, `bot/llm.py`, `bot/memory.py`, `bot/metrics.py`, `scripts/`, `tests/`, the test games folder, demo tools |
| luckydevs-2 | chat and the language-centred families | `bot/chat/`, `bot/prompts/`, `bot/opponents.py`, `bot/engines/truth.py`, `bot/engines/guardrails.py`, `bot/engines/deals.py` |
| luckydevs-3 | prep pipeline and computation engines (the hardest part) | `bot/prep/`, `bot/specialist.py`, `bot/engines/base.py`, `bot/engines/search.py`, `bot/engines/guess.py`, `bot/engines/simultaneous.py`, `bot/engines/bluff.py` |
| Sebi | integration | `main` branch, merges, shared files, freezes and pushes |

Part C, phase 1: now until the T1 freeze (Sat 18:00)

Milestones:

- As soon as the base is merged: push it as `t1`, so a valid image exists
  whatever happens later.
- 15:30: second `t1` push (L2 solid, chat working, checklist passed).
- 17:15: last merge. 17:30: final `t1` push and digest check. Nothing risky
  after that. Tag the commit.

luckydevs-1:

- [ ] Batch runner: play N practice matches over all practice games against
      the random bot and the sample bot; table with wins, warnings, timeouts,
      average and maximum answer time, cost per match.
- [ ] Harden `scripts/check_image.py` until it covers the whole checklist of
      ARENA-CONTRACT 2.9.
- [ ] Metrics: every decision appends one JSON line (game, level used, time,
      tokens, why). This feeds tuning and the presentation.
- [ ] Verify proxy behavior: model calls go straight to the gateway, anything
      else honors `HTTPS_PROXY`.
- [ ] Logo: a square `logo.png` (512 x 512) copied to `/logo.png`.

luckydevs-2:

- [ ] Chat defense: untrusted wrapper, injection detector, extraction of the
      opponent's claims as structured data passed into the decision.
- [ ] The decision prompt: robust JSON output, move and chat from one call,
      short and cache-friendly.
- [ ] Guardrails: extract the forbidden subject and its synonyms in prep;
      outgoing filter on action and chat (also the `ready` chat); a safe
      rewrite when a line is blocked.
- [ ] Truth: answer only with a quote found in the document; otherwise the
      exact "unknown" answer from the rules.

luckydevs-3:

- [ ] Game profile and family classification from the rules text.
- [ ] Simulator generation: several candidates in parallel, fixed interface.
- [ ] Sandbox: generated code runs in a child process with time and memory
      limits and can never take down the bot.
- [ ] Verification: the simulator parses every `sample_turn`; its legal
      actions equal the sample's `legal_actions`; random self-play runs
      without errors; failures are sent back to the model for repair; the
      best candidate wins.
- [ ] Engine for Think ahead: iterative deepening alpha-beta with a time
      cap, MCTS when the branching is large, solved positions cached.
- [ ] Engine for Guessing: hypothesis set filtering and best-split choice.
- [ ] Wire L3 into the ladder, only when verification passed.

Part D, phase 2: T1 until the T2 freeze (Sat 23:59)

Milestones:

- 18:00 - 20:00: watch T1, write down every lost game and why. Dinner.
- T1's 4 games are released: run them locally and read the public replays of
  the best teams.
- 23:15: final `t2` push and digest check. Tag the commit. This is the most
  important push of the event.

luckydevs-1:

- [ ] Three test games for the families the practice arena does not cover: a
      small betting game (Kuhn poker style), a negotiation or ultimatum
      game, a simultaneous-move game with a payoff twist.
- [ ] Replay analysis tool: from match replays to a table of loss reasons
      per family.
- [ ] Cost and speed report: tokens per call, cache hits, answer time; idle
      memory of a specialist with 12 alive at once.
- [ ] Memory under concurrency: many writers, cache versioning, reload.

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
- [ ] A reliable fallback action when `legal_actions` is `null`.
- [ ] Specialist cache keyed by rules hash and harness version, verified
      again on load.

Part E, phase 3: T2 until the T3 freeze (Sun 10:30) and the presentation

Milestones:

- 00:00 - 02:00: plan the night, sleep in shifts. The round robin runs by
  itself 02:00 - 06:30.
- From the round robin replays: group losses by family and fix them
  generically. T3 includes 9 games nobody has played, so generality decides.
- 09:00 - 10:30: watch T2. 10:15: final `t3` push if we are in the top 8.
  If we are not, all remaining time goes to the presentation.
- 15:30: presentation. Each member introduces themselves in about 30 seconds.

luckydevs-1:

- [ ] Demo viewer: a static page built from metrics and replays that shows
      how a specialist was built in prep and why each move was chosen.
- [ ] Numbers for the slides: verification pass rate per game, level used
      per decision, cost per match, answer times, results.
- [ ] Final freeze checks for `t3`.

luckydevs-2:

- [ ] Chat tuning from real replays: which lines worked, which injections we
      resisted, examples for the presentation.
- [ ] Slides: architecture, what failed, what we learned.

luckydevs-3:

- [ ] Run prep over every rules text we have and table the verification
      pass rate; fix the generic failures.
- [ ] Prep time budget tuning; behavior on a game of an unknown family.

Part F, pitfalls (short list): stdout purity; `match_over` is not exit; the
key changes every match; thinking tokens and empty answers; the chat rule;
only the gateway model; secrets never in Git or in the image; pushing after
the freeze changes nothing; source must match the image.

### 7.4 docs/ARENA-CONTRACT.md, AGENTS.md, CLAUDE.md

1. `docs/ARENA-CONTRACT.md`: section 2 of this file, corrected with any
   difference you found in the starter kit (mark each correction clearly).
2. `AGENTS.md` and `CLAUDE.md`: three lines each, telling any coding agent to
   read `RULES.md`, then `READMEFIRST.md`, then `docs/ARENA-CONTRACT.md`
   before changing anything, and to follow RULES.md strictly.

---------------------------------------------------------------------------

## 8. Phase 5: verification (do not skip)

Run these and show me the result of each one:

1. `python -m unittest` : all tests pass.
2. One local practice match, bot as a local command, against the random bot,
   with `BOT_FAKE_LLM=1`: no warnings, no timeouts.
3. One local practice match with the real dev key on one cheap practice game:
   check in the replay that our chat lines are there and that the opponent's
   chat appears in our model input (log the prompt to stderr at debug level).
4. `scripts/build.py dev` : the image builds for `linux/amd64`.
5. `docker run -i --rm <image>` : it starts, prints nothing and waits.
6. One practice match with prep, played by the IMAGE (the rulebook's
   `--prep` flow): `ready` in time, no warning, no timeout.
7. `scripts/keyinfo.py` : report how much of the dev key this session spent.
8. `git status` : no secret is tracked.

If a check fails, fix the cause and run it again. If you cannot fix it
quickly, tell me exactly what fails and why.

---------------------------------------------------------------------------

## 9. Phase 6: commit and report

1. Move this file to `docs/PROMPT-INITIAL.md`.
2. Commit the base on `main` in a few logical commits (ignore files first,
   then the bot, then tests and scripts, then docs). This initial setup is
   the only work that is ever committed directly to `main`.
3. Ask me before `git push origin main`.
4. Then ask me whether to push the image as `t1` right now with
   `scripts/push.py t1`, so we have a valid baseline in the registry.
5. Final report, short:
   - what exists and what is verified (the 8 checks above)
   - every difference between the starter kit and section 2
   - every default you chose without asking
   - open questions for me
   - dev key spend
   - the three commands each teammate should run first

---------------------------------------------------------------------------

## 10. Hard limits for this session

- Do not call any model other than the gateway's `haiku`.
- Do not print, log or commit any secret value.
- Do not push to Git or to the registry without asking me.
- Do not edit the practice arena engine.
- Do not write game-specific code.
- Do not add dependencies beyond the starter kit's.
- Only English and plain ASCII characters in every file.
