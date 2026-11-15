# RULES

Rules for every human and every coding agent on team `luckydevs`. Read this
file first, then `READMEFIRST.md`, then `docs/ARENA-CONTRACT.md` (and
`participant-kit/PROTOCOL.md` from the organizers' kit). These rules
are not suggestions. If a task conflicts with a rule, stop and ask your human;
your human asks Sebi.

## Git workflow

1. Each person works only on their own branches. Branch name format:
   `<name>-<feature>`, lowercase, words separated by hyphens. `<name>` is the
   person's account name (`luckydevs-1`, `luckydevs-2`, `luckydevs-3`) or
   first name; `<feature>` is a short description. Example:
   `luckydevs-2-chat-defense`.
2. Create every branch from the latest `origin/main`:
   `git fetch origin` then `git switch -c <name>-<feature> origin/main`.
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

## Ownership (reduces conflicts)

10. Edit only files in your own area:

| Person | Area | Owns these paths |
|---|---|---|
| luckydevs-1 | runtime, operations, measurement, test games | `bot/protocol.py`, `bot/clock.py`, `bot/log.py`, `bot/llm.py`, `bot/memory.py`, `bot/metrics.py`, `scripts/`, `tests/`, `testgames/` (our own test games for arena-lite), demo tools |
| luckydevs-2 | chat and the language-centred families | `bot/chat/`, `bot/prompts/`, `bot/opponents.py`, `bot/engines/truth.py`, `bot/engines/guardrails.py`, `bot/engines/deals.py` |
| luckydevs-3 | prep pipeline and computation engines (the hardest part) | `bot/prep/`, `bot/specialist.py`, `bot/engines/base.py`, `bot/engines/search.py`, `bot/engines/guess.py`, `bot/engines/simultaneous.py`, `bot/engines/bluff.py` |
| Sebi | integration | `main` branch, merges, shared files, freezes and pushes |

11. Shared files (`bot/interfaces.py`, `bot/config.py`, `bot/main.py`,
    `bot/ladder.py`, `bot/__init__.py`, `Dockerfile`, `.gitignore`,
    `.dockerignore`, `RULES.md`, `GAMEPLAN.md`, `docs/ARENA-CONTRACT.md`,
    `AGENTS.md`, `CLAUDE.md`) change only after asking Sebi's permission. If
    you need a new field or function in an interface, propose it in your
    hand-over message.
12. When you add a file or folder, add its line to `READMEFIRST.md` in the
    same commit, inside the section of your area.

## Secrets

13. Never commit `secrets.local.env`, `dev.env` or any key, token or
    password. Never paste them into code, tests, docs, logs or commit
    messages. Never copy them into the Docker image. Never `source` the
    secrets file in a shell (the Harbor robot user contains a `$`); parse it
    in Python like `scripts/_common.py` does.

## Bot invariants (breaking any of these can lose games or disqualify us)

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
    an idle specialist must use little memory and no CPU. Today the bot uses
    only the Python standard library.
24. Use the model key from the latest `prep` or `start`; never cache a
    client across matches without `set_key`.

## Testing and budget

25. Before "ready to merge": `python -m unittest` passes (with the kit
    present, so the arena-lite tests are not skipped), and at least one
    local practice match on arena-lite runs with zero warnings and zero
    timeouts (`python scripts/practice.py --game <game>` prints
    `RESULT: CLEAN`; games: `tic_tac_toe`, `pig`, `guess_number`,
    `rps_twist`, `nim_deal`). When you touched prep or the start path, also
    run `--game nim_deal --prep` (its rules text changes with every deal).
26. Use `BOT_FAKE_LLM=1` for protocol and unit tests. The dev key has a
    limited budget and 60 requests per minute shared by the whole team; do
    not run large batches without telling the team.
27. The practice arena is the organizers' `participant-kit/arena-lite`. The
    whole `participant-kit/` folder is read-only and is never committed to
    our repository (it is ignored; everyone clones it, see `READMEFIRST.md`).
    Do not edit or add anything inside it. Our own test games go in
    `testgames/`, as subclasses of `arena.core.Game`; `scripts/arena_lite.py`
    makes arena-lite see them.
28. Everything in the repository is written in English with plain ASCII
    characters.
29. When you change what prep stores in `/memory`, bump `HARNESS_VERSION` in
    `bot/config.py` (ask Sebi), or delete `memory/` in the repository root
    locally; otherwise old cached specialists are loaded.
30. What prep builds must work for a deal it has not seen: the rules text of
    a real game can differ from the one in `prep` (ARENA-CONTRACT, kit note
    K2). Never hard-code the sample deal of the prep rules or of the
    `sample_turn` in a simulator, a parser or a playbook.
