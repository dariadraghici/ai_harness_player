# PROMPT-FEATURE1-2, kit update (14 Nov, after 14:00)

Addendum to `docs/PROMPT-FEATURE1-2.md`. The organizers' participant kit
arrived after that briefing was written. Where the two differ, this file
wins. Source: `participant-kit/PROTOCOL.md`, the rulebook (5.6) and runs of
our bot on `participant-kit/arena-lite`. The full list of kit facts is in
`docs/ARENA-CONTRACT.md` (kit notes K1 to K14) on branch
`sebi-participant-kit`.

Nothing here asks you to touch a file outside your scope. Do not merge or
rebase; Sebi merges `sebi-participant-kit` and your branch (a dry-run merge
with your commit `d4fba21` is clean, and all 52 tests pass on the result;
your later, uncommitted feature 2 work was not part of that check).

## 1. Corrections to facts in the briefing

1. Section 2, fact 4 ("a sample turn has no seat"): there is still no `seat`
   field, but the sample turn is ALWAYS a turn of SEAT 0, taken from the
   middle of a sample game (each seat has moved at least twice when the game
   allows it), with an empty `chat`. Verification can call
   `parse_observation(observation, {}, 0)`. It may still be missing.
2. Section 2, fact 5 and contract point C1 ("every parameter of this variant
   is a module constant read from this rules text"): this is wrong for part
   of the games, and it is the most important change.
   - THE RULES TEXT IN `start` CAN DIFFER FROM THE RULES TEXT IN `prep`. A
     rules text may embed a setup (a position, heap sizes, a document, a
     prize order). In `prep` that setup is a SAMPLE deal. In a real game the
     setup of that game is in `start.rules` and/or `start.variant`; private
     state is in the first `turn.observation`.
   - `start.variant` is flat: `{"label": "3x3 classic", "size": 3,
     "connect": 3}`. A `prep` variant has only `label`, `rules`, `sample_turn`.
   - So split two things in the contract and in the generation prompt:
     VARIANT PARAMETERS (what makes this variant this variant: board size,
     target score, number of rounds, payoffs) may be module constants, and
     when `variant` carries a key for one of them at run time, that key wins.
     DEAL SETUP (anything that can change from game to game: a start
     position, heap sizes, dealt cards, secret values, a document) is never a
     constant; it is read from the observation in `parse_observation`.
     When in doubt, read it from the observation: every value that appears
     in the observation must come from the observation.
   - The same holds for the playbook and the profile of feature 1: they must
     not quote values of the sample deal as if they were fixed.
3. Storage key. At `start` a prepared spec is now found by game id and
   variant LABEL when the hash of the rules text misses
   (`specialist.lookup_by_label` on `sebi-participant-kit`). Such a spec has
   `source == "label"`, `spec.rules` = the rules text of the current game,
   and `spec.rules_hash` = the hash PREP stored the variant under. So
   `memory.variant_dir(spec.game_id, spec.rules_hash)`, `candidates/` and
   `analysis.json` keep working at match time. Never recompute the hash from
   `spec.rules` to find stored files.
4. `example_from_rules` had a real bug on the official Guess the Number
   rules: it returned `between 40 and 44` (an "e.g." in the scoring section)
   and missed `Examples:  "guess 50"`, so with the model down the bot lost
   the game in two answers. On `sebi-participant-kit` the regular expression
   now accepts `examples?:`. This changes "keep its current results" from
   the briefing: the new result is the right one. Your `quoted_examples`
   uses the same expression and gets the fix through the merge. Still open
   (your area): after a warning, try the other quoted examples of the rules
   before the last-resort action.

## 2. Practice and tests after the merge

- The practice arena is the organizers' arena-lite; `arena_local/` and the
  test games `tictactoe`, `guess_number` (ours) and `rps.py` are removed.
- Game ids: `tic_tac_toe`, `pig`, `guess_number` (official; `legal_actions`
  is null there), `rps_twist` (ours, simultaneous) and `nim_deal` (ours: the
  dealt heaps are written into the rules text, so `prep` and `start` differ).
- Commands: `python scripts/practice.py --game <id> [--prep] [--fake]
  [--image dev]`. `--game nim_deal --prep` is the test for point 2 above: a
  simulator that hard-codes the sample heaps is wrong there.
- Local memory is `memory/` in the repository root (not `results/memory/`).
  Fake and real runs no longer load each other's specialists: with
  `BOT_FAKE_LLM=1` the harness version has the suffix `-fake`.
- Real observations and rules to test prompts on, without playing:
  `python scripts/arena_lite.py prep --bot "x y" --game pig --show`.
- Until the merge, your branch still has the old tools; your unit tests do
  not depend on them.

## 3. What to change on your branch

1. `bot/prep/prompts/codegen_contract.txt`: rewrite C1 as in point 2 above
   (variant parameters vs deal setup; `variant` may be `{}` or the flat
   `start.variant`); in C2 say that the sample turn is seat 0.
2. The generation prompt: say that the sample observation is ONE deal of
   many, and that the module is tested later on other deals of the same
   variant.
3. The classify prompt: the playbook and `action_format` describe the
   variant, not the sample deal.
4. Mention all of this in your hand-over.
