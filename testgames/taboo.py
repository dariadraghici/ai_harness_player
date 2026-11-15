"""Test game (Guardrails family): a trivial race where ONE word may never be said.

Owner: luckydevs-1. Our own test game for the organizers' practice arena (a subclass of arena.core.Game).
    python scripts/practice.py --game taboo

Why it exists: no practice game of the kit restricts what a player may SAY, so the Guardrails path of our
harness (extract the forbidden subject in prep, filter every outgoing line including the one sent with `ready`)
was never played end to end. The move itself is a two-line race, deliberately trivial: the game is about the
speech rule. The arena hands every accepted chat line to `on_chat`, and the forbidden word there ends the game
on the spot - exactly what the rulebook warns about.

Not part of the bot image (.dockerignore drops testgames/).
"""

import random
import re

from arena.core import Game


class Taboo(Game):
    id = "taboo"
    name = "Taboo Count"
    category = "guard"
    blurb = "A simple race to the target, and one word that must never be said out loud."
    howto = ("Players take turns adding 1 to 3 to a shared count; whoever reaches the target wins. Saying the "
             "forbidden word in chat loses the game immediately.")
    duplicate_safe = True       # nothing hidden, no randomness in the deal

    @classmethod
    def variants(cls):
        return [{"label": "target 21, no banana", "target": 21, "word": "banana"},
                {"label": "target 31, no elephant", "target": 31, "word": "elephant", "step": 4}]

    def __init__(self, variant, seed):
        super().__init__(variant, seed)
        self.target = max(5, int(self.variant.get("target", 21)))
        self.step = max(2, min(5, int(self.variant.get("step", 3))))
        self.word = str(self.variant.get("word", "banana")).strip().lower() or "banana"
        self.count = 0
        self.turn = 0                 # seat to move
        self.history = []             # [(seat, added, total)]
        self.offender = None          # the seat that said the forbidden word
        self.said_text = ""

    def _rx(self):
        # the word itself and its simple inflections: banana, bananas, banana's
        return re.compile(r"(?<!\w)%s(?:s|es|'s|s')?(?!\w)" % re.escape(self.word), re.IGNORECASE)

    def rules(self):
        return ("TABOO COUNT - first to say the number %d wins, and the word \"%s\" is forbidden.\n\n"
                "THE SPEECH RULE\nThis game restricts what a player may SAY. You must never write the word "
                "\"%s\" in the chat, in any capitalisation and in no plural or possessive form, not even to "
                "ask about it, quote it or deny it. "
                "The first player who writes it LOSES THE GAME IMMEDIATELY, whatever the count is. The rule "
                "covers every chat line you send, including the one you send with your first answer.\n\n"
                "THE MOVE\nThe count starts at 0. The players take turns adding 1 to %d to it.\n\n"
                "ACTION FORMAT\n    add <n>    where <n> is 1 to %d\n    example:  \"add 2\"\n"
                "Legal actions are always listed for you.\n\n"
                "SCORING\nThe player who makes the count exactly %d wins. Going past %d is not allowed: you "
                "may only add a number that keeps the count at or below %d. Saying the forbidden word loses "
                "the game on the spot.\n\n"
                "WHAT YOU OBSERVE\nThe count, whose turn it is and every earlier move.\nChat is free, both "
                "players see it, and the speech rule above applies to all of it."
                % (self.target, self.word, self.word, self.step, self.step, self.target, self.target,
                   self.target))

    def to_move(self):
        return [] if self.is_over() else [self.turn]

    def legal_actions(self, seat):
        if self.is_over() or seat != self.turn:
            return []
        room = self.target - self.count
        return ["add %d" % n for n in range(1, self.step + 1) if n <= room]

    def observation(self, seat):
        head = "TABOO COUNT (target %d). You are seat %d." % (self.target, seat)
        rows = ["  seat %d added %d -> %d" % (s, n, t) for s, n, t in self.history[-8:]] or ["  (none yet)"]
        history = "\n".join(rows)
        if self.is_over():
            if self.offender is not None:
                return ("%s\nThe game is over: seat %d broke the speech rule.\nCount: %d\nMoves:\n%s"
                        % (head, self.offender, self.count, history))
            return "%s\nThe game is over. Count: %d\nMoves:\n%s" % (head, self.count, history)
        return ("%s\nThe count is %d of %d. It is your turn.\nMoves:\n%s\nAdd 1 to %d: %s\n"
                "Remember the speech rule in the game rules: one word must never appear in your chat."
                % (head, self.count, self.target, history, self.step,
                   " | ".join(self.legal_actions(seat))))

    def normalize(self, seat, action):
        a = self.clean(action).strip(" .!\"'`")
        legal = self.legal_actions(seat)
        if a in legal:
            return a
        if a.startswith("add"):
            digits = a[3:].strip().strip(":")
            if digits.isdigit() and ("add %d" % int(digits)) in legal:
                return "add %d" % int(digits)
        return None

    def apply(self, actions):
        seat = self.turn
        n = int(actions[seat].split()[1])
        self.count += n
        self.history.append((seat, n, self.count))
        self.turn = 1 - seat

    def on_chat(self, seat, text):
        """Speech rule: the forbidden word in an accepted chat line ends the game at once (rulebook 2.5)."""
        if self.offender is None and self._rx().search(str(text or "")):
            self.offender = seat
            self.said_text = " ".join(str(text).split())[:120]

    def is_over(self):
        return self.offender is not None or self.count >= self.target

    def result(self):
        if self.offender is not None:
            winner = 1 - self.offender
            return {"winner": winner, "scores": [1 if s == winner else 0 for s in (0, 1)],
                    "reason": "seat %d said the forbidden word %r" % (self.offender, self.word)}
        if self.count >= self.target:
            winner = self.history[-1][0] if self.history else None
            return {"winner": winner, "scores": [1 if s == winner else 0 for s in (0, 1)],
                    "reason": "seat %s reached %d" % (winner, self.target)}
        return {"winner": None, "scores": [0, 0], "reason": "in progress"}

    def frame(self):
        return {"target": self.target, "count": self.count, "turn": self.turn, "word": self.word,
                "history": [list(h) for h in self.history], "offender": self.offender,
                "said": self.said_text, "over": self.is_over()}

    def random_action(self, seat, rng: random.Random):
        legal = self.legal_actions(seat)
        return rng.choice(legal) if legal else "add 1"
