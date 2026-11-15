"""Test game (Deals family): alternating offers over a shrinking pot with private values.

Owner: luckydevs-1. Our own test game for the organizers' practice arena (a subclass of arena.core.Game).
    python scripts/practice.py --game split_deal

Why it exists: no practice game of the kit is a negotiation, so the Deals path of our harness (our own value,
a reservation price, a concession schedule, code deciding accept or reject while the model writes the words)
was never played end to end. The pot shrinks every round, so waiting costs something, and each player's
multiplier is private, so neither can compute the other's score: the chat is the only channel for it.

Not part of the bot image (.dockerignore drops testgames/).
"""

import random

from arena.core import Game


class SplitDeal(Game):
    id = "split_deal"
    name = "Split the Deal"
    category = "deals"
    blurb = "A pot that shrinks while you argue, and only you know what it is worth to you."
    howto = ("The players take turns proposing how to split a pot of points; the other accepts or rejects. "
             "Every rejection shrinks the pot. Each player has a private multiplier on what it receives.")
    duplicate_safe = False      # private multipliers (arena.core.Game.duplicate_safe)

    @classmethod
    def variants(cls):
        return [{"label": "pot 20, 6 offers", "pot": 20, "offers": 6, "decay": 2},
                {"label": "pot 30, 4 offers", "pot": 30, "offers": 4, "decay": 4}]

    def __init__(self, variant, seed):
        super().__init__(variant, seed)
        self.pot0 = max(4, int(self.variant.get("pot", 20)))
        self.offers = max(2, int(self.variant.get("offers", 6)))
        self.decay = max(0, int(self.variant.get("decay", 2)))
        self.value = [self.rng.choice([1, 2, 3]), self.rng.choice([1, 2, 3])]   # private multiplier per seat
        self.history = []            # [{"round", "proposer", "keep", "pot", "answer"}]
        self.round = 1
        self.pending = None          # the offer waiting for an answer: (proposer, keep)
        self.deal = None             # (points to seat 0, points to seat 1) once accepted
        self._over = False

    def pot(self):
        return max(0, self.pot0 - self.decay * (self.round - 1))

    def proposer(self):
        return (self.round - 1) % 2

    # ------------------------------------------------------------------ the contract
    def rules(self):
        return ("SPLIT THE DEAL - pot %d, %d offers, the pot shrinks by %d after every rejection.\n\n"
                "SETUP\nThere is a pot of %d points. Each player also has a PRIVATE multiplier (1, 2 or 3) "
                "that is applied to the points it receives; you know only your own. Nobody is told the other "
                "multiplier at any time.\n\n"
                "TURN ORDER\nSeat 0 makes the first offer, then the seats alternate. An offer is answered by "
                "the other player before the next offer is made. After %d offers without an accepted deal the "
                "negotiation fails.\n\n"
                "ACTION FORMAT\n"
                "    as the proposer:  offer <n>    where <n> is how many points YOU keep, 0 to the pot size\n"
                "                      example:  \"offer 12\"\n"
                "    as the responder: exactly accept | reject\n"
                "Legal actions are always listed for you.\n\n"
                "SCORING\nWhen an offer is accepted the proposer scores (kept points x its own multiplier) and "
                "the responder scores (pot minus kept points, x its own multiplier). After a rejection the pot "
                "shrinks by %d and the other player makes the next offer. With no deal both players score 0 "
                "and the game is a draw. The higher score wins.\n\n"
                "WHAT YOU OBSERVE\nYour own multiplier, the pot of this round, every earlier offer with its "
                "answer, and whose turn it is.\nChat is free and both players see it." % (
                    self.pot0, self.offers, self.decay, self.pot0, self.offers, self.decay))

    def to_move(self):
        if self.is_over():
            return []
        return [1 - self.pending[0]] if self.pending else [self.proposer()]

    def legal_actions(self, seat):
        if self.is_over() or seat not in self.to_move():
            return []
        if self.pending:
            return ["accept", "reject"]
        return self.cap_list(["offer %d" % n for n in range(0, self.pot() + 1)])

    def observation(self, seat):
        head = "SPLIT THE DEAL. You are seat %d." % seat
        mine = "Your private multiplier: %d (the opponent's is unknown to you)." % self.value[seat]
        rows = []
        for h in self.history:
            rows.append("  Round %d: seat %d offered to keep %d of %d -> %s"
                        % (h["round"], h["proposer"], h["keep"], h["pot"], h["answer"]))
        history = "\n".join(rows) or "  (none yet)"
        if self.is_over():
            if self.deal:
                got = self.deal[seat]
                tail = "The deal gave you %d points, so %d x %d = %d." % (got, got, self.value[seat],
                                                                          got * self.value[seat])
            else:
                tail = "No deal was made, so both players score 0."
            return "%s\nThe game is over. %s\n%s\nOffers:\n%s" % (head, tail, mine, history)
        if self.pending:
            proposer, keep = self.pending
            ask = ("Seat %d offers to keep %d of the %d points, which leaves you %d. Answer accept or reject."
                   % (proposer, keep, self.pot(), self.pot() - keep))
        else:
            ask = ("It is your offer. The pot is %d points; say how many you keep, for example \"offer %d\"."
                   % (self.pot(), self.pot() // 2))
        return ("%s\nRound %d of %d. Pot now %d (it shrinks by %d after a rejection).\n%s\nOffers:\n%s\n%s"
                % (head, self.round, self.offers, self.pot(), self.decay, mine, history, ask))

    def normalize(self, seat, action):
        a = self.clean(action).strip(" .!\"'`")
        legal = self.legal_actions(seat)
        if a in legal:
            return a
        if self.pending is None and a.startswith("offer"):
            digits = a[5:].strip().strip(":")
            if digits.isdigit() and ("offer %d" % int(digits)) in legal:
                return "offer %d" % int(digits)
        return None

    def apply(self, actions):
        seat = self.to_move()[0]
        action = actions[seat]
        if self.pending is None:
            self.pending = (seat, int(action.split()[1]))
            return
        proposer, keep = self.pending
        pot = self.pot()
        self.history.append({"round": self.round, "proposer": proposer, "keep": keep, "pot": pot,
                             "answer": action})
        self.pending = None
        if action == "accept":
            self.deal = [keep if s == proposer else pot - keep for s in (0, 1)]
            self._over = True
            return
        self.round += 1
        if self.round > self.offers or self.pot() <= 0:
            self._over = True

    def is_over(self):
        return self._over

    def scores(self):
        if not self.deal:
            return [0, 0]
        return [self.deal[s] * self.value[s] for s in (0, 1)]

    def result(self):
        s0, s1 = self.scores()
        winner = None if s0 == s1 else (0 if s0 > s1 else 1)
        reason = ("deal %d/%d, scores %d-%d" % (self.deal[0], self.deal[1], s0, s1) if self.deal
                  else "no deal after %d round(s)" % len(self.history))
        return {"winner": winner if self.is_over() else None, "scores": [s0, s1], "reason": reason}

    def frame(self):
        return {"pot": self.pot(), "round": self.round, "offers": self.offers, "value": list(self.value),
                "history": [dict(h) for h in self.history], "pending": list(self.pending) if self.pending else None,
                "deal": list(self.deal) if self.deal else None, "scores": self.scores(), "over": self.is_over()}

    def random_action(self, seat, rng: random.Random):
        legal = self.legal_actions(seat)
        if not legal:
            return "reject"
        if self.pending:
            return rng.choice(legal)
        return "offer %d" % rng.randint(self.pot() // 3, self.pot())
