"""Test game (Bluff family): a two-card betting game with hidden hands.

Owner: luckydevs-1. Our own test game for the organizers' practice arena (a subclass of arena.core.Game, as
participant-kit/arena-lite/README.md asks). scripts/arena_lite.py adds this folder to the arena's games, so:
    python scripts/practice.py --game kuhn_bet

Why it exists: no practice game of the kit has hidden information, so the Bluff path of our harness (sampling
the hidden states, expected value per action, a mixed strategy) was never played end to end. This is a
Kuhn-poker shape, the smallest game where bluffing is actually correct: with three cards the equilibrium bets
the worst card some of the time.

Not part of the bot image (.dockerignore drops testgames/).
"""

import random

from arena.core import Game

NAMES = {1: "Jack", 2: "Queen", 3: "King", 4: "Ace"}


class KuhnBet(Game):
    id = "kuhn_bet"
    name = "Kuhn Bet"
    category = "bluff"
    blurb = "One card each, one bet: the smallest game where bluffing with the worst hand is the right play."
    howto = ("Each player is dealt one hidden card. Seat 0 may check or bet one chip; a bet can be called or "
             "folded. The higher card wins the pot at a showdown.")
    duplicate_safe = False      # hidden cards (arena.core.Game.duplicate_safe)

    @classmethod
    def variants(cls):
        return [{"label": "3 cards, 4 hands", "cards": 3, "hands": 4},
                {"label": "4 cards, 6 hands", "cards": 4, "hands": 6}]

    def __init__(self, variant, seed):
        super().__init__(variant, seed)
        self.cards = max(3, min(4, int(self.variant.get("cards", 3))))
        self.hands = max(1, int(self.variant.get("hands", 4)))
        self.chips = [0, 0]                 # net chips won, per seat
        self.done = []                      # finished hands: dicts for the observation and the frame
        self.hand = 0
        self._deal()

    # ------------------------------------------------------------------ one hand
    def _deal(self):
        deck = list(range(1, self.cards + 1))
        self.rng.shuffle(deck)
        self.hole = [deck[0], deck[1]]      # the hidden card of each seat
        self.dealer = self.hand % 2         # the seat that acts first in this hand
        self.actions = []                   # [(seat, action)] of the hand in progress
        self.pot = 2                        # one ante from each seat
        self.bet_by = None

    def _hand_over(self):
        if not self.actions:
            return False
        last = self.actions[-1][1]
        if last in ("fold", "call"):
            return True
        return len(self.actions) == 2 and all(a == "check" for _s, a in self.actions)

    def _settle(self):
        """Pay the finished hand and record it. Both cards become public once the hand is over."""
        first, second = self.dealer, 1 - self.dealer
        last = self.actions[-1][1]
        folded = None
        if last == "fold":
            folded = self.actions[-1][0]
        if folded is not None:
            winner = 1 - folded
            gain = 1 + (1 if any(a == "bet" for _s, a in self.actions) else 0)
            # the folder loses its ante (and its call is never made)
            self.chips[winner] += 1
            self.chips[folded] -= 1
        else:
            stake = 2 if last == "call" else 1          # chips at risk per seat
            winner = first if self.hole[first] > self.hole[second] else second
            self.chips[winner] += stake
            self.chips[1 - winner] -= stake
        self.done.append({"hand": self.hand + 1, "dealer": self.dealer,
                          "cards": [self.hole[0], self.hole[1]],
                          "actions": [[s, a] for s, a in self.actions], "winner": winner,
                          "chips": list(self.chips)})
        self.hand += 1
        if self.hand < self.hands:
            self._deal()

    # ------------------------------------------------------------------ the contract
    def rules(self):
        high = NAMES[self.cards]
        order = " < ".join(NAMES[i] for i in range(1, self.cards + 1))
        return ("KUHN BET - %d cards, %d hands.\n\n"
                "SETUP\nEach hand both players put one chip in the pot (the ante) and are dealt ONE hidden card "
                "from a deck of %d: %s. You see your own card only. The deck is reshuffled every hand.\n\n"
                "TURN ORDER\nThe dealer acts first and alternates every hand (seat 0 deals the first hand).\n\n"
                "ACTION FORMAT\n    exactly one of: check | bet | call | fold (lower case). Example:  \"bet\"\n"
                "The first player may check or bet. After a check the other may check (showdown) or bet. "
                "After a bet the other must call or fold. Legal actions are always listed for you.\n\n"
                "SCORING\nA bet is one chip. A fold gives the pot to the other player: the folder loses its "
                "ante. At a showdown the higher card wins every chip at risk (1 after two checks, 2 after a "
                "bet and a call). %s is the highest card.\nMost chips after %d hands wins; equal chips is a "
                "draw.\n\n"
                "WHAT YOU OBSERVE\nYour own card, the chips of both players, the actions of the hand in "
                "progress, and every finished hand with BOTH cards revealed.\nChat is free and both players "
                "see it." % (self.cards, self.hands, self.cards, order, high, self.hands))

    def to_move(self):
        if self.is_over():
            return []
        return [self.dealer if not self.actions else 1 - self.actions[-1][0]]

    def legal_actions(self, seat):
        if self.is_over() or seat not in self.to_move():
            return []
        if not self.actions:
            return ["check", "bet"]
        last = self.actions[-1][1]
        if last == "check":
            return ["check", "bet"]
        return ["call", "fold"]

    def observation(self, seat):
        head = "KUHN BET (%d cards). You are seat %d." % (self.cards, seat)
        card = "Your hidden card: %d (%s)." % (self.hole[seat], NAMES[self.hole[seat]])
        chips = "Chips: you %+d, opponent %+d." % (self.chips[seat], self.chips[1 - seat])
        rows = []
        for h in self.done[-6:]:
            acts = ", ".join("seat %d %s" % (s, a) for s, a in h["actions"])
            rows.append("  Hand %d (dealer seat %d): your card %d, opponent card %d, %s -> seat %d won"
                        % (h["hand"], h["dealer"], h["cards"][seat], h["cards"][1 - seat], acts, h["winner"]))
        history = "\n".join(rows) or "  (none yet)"
        if self.is_over():
            return "%s\nThe game is over. %s\nFinished hands:\n%s" % (head, chips, history)
        now = ", ".join("seat %d %s" % (s, a) for s, a in self.actions) or "(you act first)"
        return ("%s\nHand %d of %d, dealer is seat %d. %s\n%s\nThis hand so far: %s\nFinished hands:\n%s\n"
                "Your turn: %s" % (head, self.hand + 1, self.hands, self.dealer, card, chips, now, history,
                                   " | ".join(self.legal_actions(seat))))

    def normalize(self, seat, action):
        a = self.clean(action).strip(" .!\"'`")
        return a if a in self.legal_actions(seat) else None

    def apply(self, actions):
        seat = self.to_move()[0]
        self.actions.append((seat, actions[seat]))
        if self._hand_over():
            self._settle()

    def is_over(self):
        return self.hand >= self.hands

    def result(self):
        c0, c1 = self.chips
        winner = None if c0 == c1 else (0 if c0 > c1 else 1)
        return {"winner": winner if self.is_over() else None, "scores": [c0, c1],
                "reason": "chips %+d / %+d after %d hand(s)" % (c0, c1, len(self.done))}

    def frame(self):
        return {"cards": self.cards, "hands": self.hands, "hand": self.hand + 1 if not self.is_over() else self.hands,
                "hole": list(self.hole), "pot": self.pot, "chips": list(self.chips),
                "actions": [[s, a] for s, a in self.actions], "done": [dict(h) for h in self.done],
                "over": self.is_over()}

    def random_action(self, seat, rng: random.Random):
        legal = self.legal_actions(seat)
        return rng.choice(legal) if legal else "check"
