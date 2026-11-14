"""Shared data classes and interfaces: the contracts between modules.

Shared file (owner: Sebi). Change only with permission, see RULES.md. If you
need a new field, propose it in your hand-over message.
"""

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Protocol

# The 7 game families of the event, plus "unknown".
FAMILIES = (
    "bluff",          # hidden information, betting, bluffing, probabilities
    "deals",          # negotiation, trust and betrayal, auctions with private values
    "read_opponent",  # simultaneous moves, patterns, mixed strategies
    "guessing",       # deduction from feedback
    "think_ahead",    # perfect-information board games, search, tactics
    "truth",          # answer from a given document; invented answer costs more than "unknown"
    "guardrails",     # rules name a forbidden subject; never mention it
    "unknown",
)


@dataclass
class GameProfile:
    """What prep learned about one variant of a game."""
    family: str = "unknown"
    perfect_information: Optional[bool] = None
    simultaneous: Optional[bool] = None
    speech_restrictions: str = ""          # free text, e.g. the forbidden subject
    action_format: str = ""                # exact format from the rules text
    example_action: str = ""               # one valid action, used by L1 when legal_actions is null
    notes: str = ""
    chat_is_move: bool = False             # talking IS the move (kind "move" chat, free-text actions); see chat/offense.py

    @staticmethod
    def from_dict(d: Optional[Dict[str, Any]]) -> "GameProfile":
        d = d or {}
        fam = str(d.get("family") or "unknown").strip().lower()
        if fam not in FAMILIES:
            fam = "unknown"
        return GameProfile(
            family=fam,
            perfect_information=_opt_bool(d.get("perfect_information")),
            simultaneous=_opt_bool(d.get("simultaneous")),
            speech_restrictions=str(d.get("speech_restrictions") or ""),
            action_format=str(d.get("action_format") or ""),
            example_action=str(d.get("example_action") or ""),
            notes=str(d.get("notes") or ""),
            chat_is_move=bool(d.get("chat_is_move")),
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _opt_bool(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.strip().lower() in ("true", "false"):
        return v.strip().lower() == "true"
    return None


@dataclass
class Candidate:
    """One move proposed by an engine (L3). Higher value is better for us."""
    action: str
    value: float
    note: str = ""


@dataclass
class Decision:
    """What we answer to a turn. level: 3 engine, 2 model validated, 1 safe."""
    action: str
    chat: str
    level: int
    why: str = ""
    goal: Optional["InfluenceGoal"] = None   # the chat objective this chat line serves (chat/offense.py), if any


@dataclass
class InfluenceGoal:
    """What the chat line of one turn is meant to do (PROMPT-CHAT section 4). Built in code, never by the model."""
    kind: str = "quiet"            # induce_move, deter_move, induce_belief, build_credibility, quiet
    tactic_id: str = "quiet"
    target_action: str = ""        # the opponent action we try to induce or deter (may be filled from the model answer)
    belief: str = ""               # the belief we try to install, as an intent for the model
    gap: Optional[float] = None    # value gap between their likely and their worst reply; None = no engine
    confidence: float = 0.0
    is_control: bool = False       # a deliberate quiet turn that stands in for `shadow_tactic` (baseline)
    shadow_tactic: str = ""
    reveal: bool = False           # a deliberate reveal of private state (the leak check is relaxed)
    lag: int = 0                   # rounds before the prediction can be scored (1 in simultaneous games)
    opp_class: str = "unknown"
    claim_subject: str = ""        # ledger key of the claim this line makes ("" = no claim)
    claim_value: str = ""
    claim_scope: str = "game"      # "game" or "match"
    allow_claims: bool = False     # the line may state an intent or a plan (it is a tactic, not a leak)
    alt_tactic: str = ""           # resolved from the chosen action in code (bluff: strength_claim when chips go in)
    alt_when: str = ""             # regex on the action that switches to alt_tactic
    resend: bool = False           # the previous answer was rejected: this is the same goal again
    # phase 2: what the engine said about their replies to the move we play (chat/offense.py analyze_replies)
    avoid_action: str = ""         # their likely reply (best for them): the one we try to deter or steer away from
    from_engine: bool = False      # target_action / avoid_action come from the engine, not from the model
    engine_action: str = ""        # the move of ours that the analysis assumes (the engine's top candidate)
    replies: Optional[dict] = None # the reply table (rows with value for us) kept for their_quality_drop
    promise_if: str = ""           # commitment: when they play this ...
    promise_then: str = ""         # ... we answer with this (their best response, so the promise costs us nothing)
    arm_info: Optional[dict] = None  # how the bandit chose (for the log), never sent anywhere
    game: Any = None
    turn: Any = None


@dataclass
class Outcome:
    """The scored result of one influence goal, one decision (or one round, in simultaneous games) later."""
    tactic_id: str = ""
    scored: bool = False           # False: nothing to score (warning, no target, not yet)
    hit: Optional[bool] = None
    their_action: str = ""
    their_quality_drop: Optional[float] = None
    drop_exact: Optional[bool] = None   # every reply value behind the drop was exact (a solved tree), not an estimate
    lag: int = 0
    reason: str = ""
    is_control: bool = False


@dataclass
class ChatLine:
    sender: str          # "you" or "opponent"
    text: str
    game: Any = None
    turn: Any = None
    phase: str = ""
    kind: str = "chat"
    flags: List[str] = field(default_factory=list)   # set by chat/defense.py


@dataclass
class ChatContext:
    """The chat so far, split into ours and theirs, with injection flags."""
    ours: List[ChatLine] = field(default_factory=list)
    theirs: List[ChatLine] = field(default_factory=list)
    all_lines: List[ChatLine] = field(default_factory=list)   # oldest first, both sides
    injection_suspected: bool = False


@dataclass
class VariantSpec:
    """Everything prep built for one variant (one rules text)."""
    game_id: str
    game_name: str
    label: str
    rules: str
    rules_hash: str
    profile: GameProfile = field(default_factory=GameProfile)
    playbook: str = ""
    sample_turn: Optional[Dict[str, Any]] = None
    simulator_path: Optional[str] = None     # set by prep when a simulator was generated
    verified: bool = False                   # True only when prep/verify.py passed
    source: str = "none"                     # "prep", "memory", "cold", "default"


class Engine(Protocol):
    """An L3 engine for one family. Must respect the deadline (time.monotonic())."""

    def candidates(self, specialist: VariantSpec, turn_msg: Dict[str, Any],
                   deadline: float) -> List[Candidate]:
        ...


# ----------------------------------------------------------------------------
# Contract of a generated simulator module (DRAFT, owner luckydevs-3 may refine).
#
# prep/codegen.py asks the model to write one Python file that defines these
# top-level functions. The harness only calls them inside prep/sandbox.py
# (a killable child process), never in the bot process.
#
#   parse_observation(observation: str, variant: dict, seat: int) -> state
#       Build a state from the arena's observation text. Hidden information
#       that we cannot see is left unknown (bluff engines sample it).
#   legal_actions(state) -> list[str]
#       Exact arena spelling of every legal action for current_player(state).
#   apply_action(state, action: str) -> state
#       Return the next state. Must not mutate the input.
#   is_terminal(state) -> bool
#   returns(state) -> list[float]
#       Final value per seat [seat0, seat1]; only meaningful when terminal.
#   current_player(state) -> int
#       0 or 1; -1 for a simultaneous step (both move).
#
# state must be JSON-serializable (dict/list/str/int/float/bool/None) so it
# can cross the process boundary.
# ----------------------------------------------------------------------------
SIMULATOR_FUNCTIONS = (
    "parse_observation",
    "legal_actions",
    "apply_action",
    "is_terminal",
    "returns",
    "current_player",
)
