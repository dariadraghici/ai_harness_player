# Arena contract

Section 2 below is section 2 of `docs/PROMPT-INITIAL.md` (our summary of the
rulebook), copied UNCHANGED. Read it before changing anything in `bot/`.

Since 14 Nov, 14:00 we also have the organizers' PARTICIPANT KIT. It is the
authority; where it says more than section 2, the kit wins. It is not in our
Git (it is its own repository); clone it into the repository root:

```
git clone https://git.hackaton-2026.dvloper.io/arena/participant-kit.git participant-kit
```

| In the kit | What it is |
|---|---|
| `participant-kit/PROTOCOL.md` | the message protocol (5 minutes; read it) |
| `participant-kit/README.md` | the event: scoring, tournaments, prep, chat rule, model, submitting |
| `participant-kit/CHANGELOG.md` | every change after kick-off |
| `participant-kit/docs/AI-Harness-Arena-Rulebook.pdf` | the Candidate Rulebook, with the rule numbers the judges cite |
| `participant-kit/arena-lite/` | the practice arena: the same match engine the organizers run, 3 practice games |
| `participant-kit/starter/` | the sample bot (one model call per move) |

## Kit notes (read first)

Checked on 14 Nov against the kit and by running our bot on arena-lite.
Section 2 contains nothing that the kit contradicts; these are the points the
kit adds or makes precise. K1 to K4 change how we must build things.

- [K1] `sample_turn` is always a turn of SEAT 0, from the middle of a sample
  game (each seat has moved at least twice when the game allows it), with an
  empty `chat`. It has no `seat` field. It comes from a sample deal, never
  from a real game. It may be missing, and it does not always hold the full
  history.
- [K2] THE RULES TEXT IN `start` CAN DIFFER FROM THE RULES TEXT IN `prep`.
  Rules texts may embed a setup (a position, heap sizes, a document, a prize
  order). In `prep` that is a sample deal; in a real game the setup of THIS
  game is in `start.rules` and/or `start.variant`, private state (cards,
  secret number, hand) is in the first `turn.observation`, public history is
  in `observation` and in the chat. There is no separate deal field
  (rulebook 5.6). Consequences for us:
  - a specialist keyed only by the hash of the rules text is not found at
    `start` in such games. `bot/specialist.py` therefore falls back to the
    same game and variant LABEL (`lookup_by_label`), and the decision always
    gets the rules text of the current game;
  - a generated simulator, a playbook or a parser must never hard-code the
    sample deal: everything that belongs to the deal is read from the
    observation (or from `start.rules` / `start.variant`) at run time.
- [K3] `start.variant` is flat: the label plus the settings as top-level keys,
  for example `{"label": "3x3 classic", "size": 3, "connect": 3}`. A `prep`
  variant has only `label`, `rules` and (usually) `sample_turn`; the settings
  arrive with `start`. The decision prompt includes the variant settings.
- [K4] How an action is judged: the engine normalizes it per game (typically
  lower case, trimmed, single spaces), a number is accepted as a string, and
  an empty or missing `action` is a bad answer. `legal_actions` shows the
  arena's spelling; we always send that spelling.
- [K5] Chat details: `chat` in our answers is technically optional for the
  engine but mandatory by the rules (2.5); the arena collapses whitespace and
  cuts at 200 characters; an empty string is ignored; `comment` is an alias.
  A line sent with a move reaches the opponent in its next message. The chat
  on a rejected (warned) answer is dropped. `from: "you"` means our TEAM:
  another of our containers (another game of the match) may have said it.
- [K6] After each freeze the organizers may run a chat check: their bot plays
  our frozen image in two games of Tic-Tac-Toe and makes small talk; they
  note whether we chat back and whether its words show up in our model calls
  (the gateway records the requests). It scores nothing but guides the code
  review. Our decision call always contains the opponent's chat block.
- [K7] Clocks and failures: a re-sent turn has a fresh clock; during `prep`
  invalid lines are ignored; a cold container keeps the 120 s study clock for
  the rest of the tournament; after a crash the next game of that kind is
  played by a fresh cold container; `shutdown` is followed by a closed stdin
  and 15 s to exit. The engine adds 0.5 s of transport slack to every clock;
  we do not use it.
- [K8] Thinking can overrun its budget by a lot (a 5000 token budget was seen
  to use 9000 tokens and return no text). Keep generous `max_tokens` headroom
  and always handle an empty answer. Arena keys have no per-minute limit; the
  dev key has 60 requests per minute.
- [K9] Matches: best of 3 from six drawn slots (three regular games, all
  different, plus up to three sudden-death games); a 2-0 lead stops the match,
  a 1.5-0.5 lead does not; in sudden death the first game won decides. Still
  tied: fewer warnings, then less total answer time (accepted `ready` and
  accepted actions only), then lower model cost, then a coin flip. Round
  robin: 2 boards = 4 games, `best_of` 4, all played, games 1-2 and 3-4 are
  the same game and variant with seats swapped; hidden-information games get
  a fresh deal for the second game; a tied match stays a draw.
- [K10] T3: the organizers draw six slots per match and our image is prepared
  only for the games drawn for our match (not all 21). Four of the 21 games
  are reserved for the final. T1's 4 games are added to arena-lite after T1,
  T2's 8 after T2 (the organizers hand out the updated kit: `git pull` in
  `participant-kit/`).
- [K11] Scoring formulas: cost = 2.5 x (cheapest team's average match spend /
  ours); only match keys count, prep spend does not. Robustness = 2.5 minus
  1.25 for every game lost by timeout, invalid JSON, wrong type, illegal or
  missing action, crash, absent image or forfeit; a warning alone costs
  nothing there.
- [K12] Other rules: no human may look at or touch a bot, its `/memory` or
  its logs in the arena after kick-off (7.8); remote MCP servers only for
  non-game content (7.11); abusive chat includes sexual content (13.1); the
  logo may also be `/logo.webp` or `/logo.jpg`; extra environment variable
  `NODE_USE_ENV_PROXY=1`.
- [K13] The Practice page `https://arena.hackaton-2026.dvloper.io/#/practice`:
  sign in with the practice token, pick the tag of an image we PUSHED, a game
  and an opponent (Prompt Only, dvloper House, dvloper Master, our own image,
  or a human in the browser). One run at a time per team, 10 runs per team
  per hour, 1 to 3 games per run, closed while a tournament or its prep runs.
  Nothing there counts for the score. This replaces the old [OPEN] note.
- [K14] Local practice: every mention of `arena-lite` and `python -m arena` in
  section 2 now means `participant-kit/arena-lite`. We run it through
  `python scripts/practice.py` (see `READMEFIRST.md`). Our own emulator
  `arena_local/` was removed on 14 Nov. The bot still uses only the Python
  standard library (`urllib`) for the gateway; the starter's `openai` pin
  (`openai>=3,<4`) is not needed.
- [VERIFIED, 14 Nov] Dev key: `GET https://llm.hackaton-2026.dvloper.io/key/info`
  answers `{"info": {"max_budget": 6.0, "spend": ..., "rpm_limit": 60, "models": ["haiku"], ...}}`.
- [VERIFIED, 14 Nov] A call with `"thinking": {"type": "disabled"}` and
  `max_tokens` 20 returns content and a `usage` object with `prompt_tokens`,
  `completion_tokens` and `prompt_tokens_details.cached_tokens`.
- [VERIFIED, 14 Nov] Our bot on arena-lite (local command and image, with and
  without `--prep`, fake and real model): no warning, no timeout, no stray
  stdout line, chat on every answer.

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
