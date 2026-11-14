# Arena contract

This file is section 2 of `docs/PROMPT-INITIAL.md` (the authoritative summary
of the rulebook), copied UNCHANGED below the line. Read it before changing
anything in `bot/`.

## Corrections and notes (read first)

- [NOTE, 14 Nov] The rulebook says a starter kit exists (a sample bot
  `starter/main.py` with its Dockerfile, and the practice arena `arena-lite/`,
  run with `python -m arena ...`). NO starter kit was provided to this
  repository. Therefore:
  - there are no differences to report between a starter kit and this
    contract; this contract is the only source;
  - every mention of `arena-lite`, `python -m arena` and `--env-file ../dev.env`
    below refers to the official practice arena, which we do not have. Locally
    we use OUR emulator `arena_local/` (`python -m arena_local practice ...`),
    written from this contract only. Where the contract is silent, the emulator
    makes simple choices (listed in `arena_local/runner.py`). Behavior of the
    real arena may differ in those places;
  - there is no `openai` version pin to copy: the bot uses only the Python
    standard library (`urllib`) to call the OpenAI-compatible gateway, with
    the same request body as the example in 2.6 (`extra_body` fields are sent
    as top-level JSON fields, which is what the `openai` library does).
- [VERIFIED, 14 Nov] Dev key: `GET https://llm.hackaton-2026.dvloper.io/key/info`
  answers `{"info": {"max_budget": 6.0, "spend": ..., "rpm_limit": 60, "models": ["haiku"], ...}}`.
- [VERIFIED, 14 Nov] A call with `"thinking": {"type": "disabled"}` and
  `max_tokens` 20 returns content and a `usage` object with `prompt_tokens`,
  `completion_tokens` and `prompt_tokens_details.cached_tokens`.
- [OPEN] The practice page (`https://arena.hackaton-2026.dvloper.io/#/practice`)
  needs a practice token (`PRACTICE_TOKEN` in `secrets.local.env`). How it is
  used is not documented here; Sebi checks it by hand.

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
