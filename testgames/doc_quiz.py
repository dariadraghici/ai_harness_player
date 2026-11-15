"""Test game (Truth family): answer only from the document, or say exactly "unknown".

Owner: luckydevs-1. Our own test game for the organizers' practice arena (a subclass of arena.core.Game).
    python scripts/practice.py --game doc_quiz

Why it exists: no practice game of the kit is a document-question game, so the Truth path of our harness
(answer only with a phrase the document supports, otherwise the exact unknown answer) was never played end to
end. Some questions deliberately have NO answer in the document: inventing one costs more than admitting it,
which is exactly the trade-off the family is about. Both players answer the same question at the same time, so
neither can learn the answer from the other's move.

Not part of the bot image (.dockerignore drops testgames/).
"""

import random

from arena.core import Game

# (question, the phrase the document supports, or "" when the document does not answer it)
BANK = [
    ("In which year was the lighthouse at Brae Head first lit?", "1847"),
    ("What is the name of the keeper who kept the log?", "Alasdair Finn"),
    ("How many steps does the tower have?", "214"),
    ("What colour was the original lamp housing?", "dark green"),
    ("Which ship ran aground in the storm of 1861?", "the Cordelia"),
    ("How far can the light be seen in clear weather?", "eighteen nautical miles"),
    ("What is the name of the keeper's dog?", ""),
    ("How much did the tower cost to build?", ""),
    ("Which company supplied the lamp oil after 1870?", ""),
    ("What was the population of the village in 1847?", ""),
]
DOCUMENT = """BRAE HEAD LIGHTHOUSE - A SHORT RECORD

The lighthouse at Brae Head was first lit in 1847, on the night of the autumn equinox. The tower is built of
local granite and has 214 steps from the door to the lamp room. Its original lamp housing was painted dark
green, a colour chosen so that it would not be mistaken for the red housing at Carrick Point.

The keeper who kept the log for the first twenty years was Alasdair Finn. His entries are brief and almost
always about the weather. The log for the storm of 1861 records that the Cordelia ran aground on the southern
reef, and that every one of her crew reached the shore.

In clear weather the light can be seen eighteen nautical miles out to sea. The lamp was converted to electricity
much later, and the tower has been unmanned since then.
"""


class DocQuiz(Game):
    id = "doc_quiz"
    name = "Document Quiz"
    category = "truth"
    blurb = "Answer from the document or admit you cannot: a confident invention costs more than \"unknown\"."
    howto = ("Both players get the same document and the same question each round. A supported answer scores, "
             "\"unknown\" scores nothing, and an answer the document does not support loses a point.")
    duplicate_safe = True       # the document and the questions are public to both seats, nothing is hidden

    @classmethod
    def variants(cls):
        return [{"label": "6 questions", "questions": 6},
                {"label": "10 questions, strict", "questions": 10, "penalty": 2}]

    def __init__(self, variant, seed):
        super().__init__(variant, seed)
        self.n = max(1, min(len(BANK), int(self.variant.get("questions", 6))))
        self.penalty = max(0, int(self.variant.get("penalty", 1)))
        order = list(range(len(BANK)))
        self.rng.shuffle(order)
        self.asked = order[:self.n]
        self.round = 0
        self.points = [0, 0]
        self.log = []               # [{"question", "expected", "answers": [a0, a1], "points": [p0, p1]}]

    def question(self):
        return BANK[self.asked[self.round]] if self.round < self.n else ("", "")

    def rules(self):
        return ("DOCUMENT QUIZ - %d questions, both players answer every question.\n\n"
                "THE DOCUMENT\n%s\n"
                "HOW A ROUND WORKS\nBoth players are asked the same question at the same time and answer "
                "without seeing the other answer.\n\n"
                "ACTION FORMAT\n    free text: the answer itself, as short as possible, in the words of the "
                "document\n    or exactly:  unknown\n    examples:  \"214\"   \"unknown\"\n"
                "There is no list of legal actions.\n\n"
                "SCORING\n+2 for an answer the document supports.\n 0 for exactly \"unknown\".\n-%d for an "
                "answer the document does not support.\nSome questions cannot be answered from the document; "
                "for those, \"unknown\" scores +1 and anything else loses %d.\nMost points after %d questions "
                "wins; equal points is a draw.\n\n"
                "WHAT YOU OBSERVE\nThe document (above, it never changes), the current question, both scores "
                "and every earlier question with both answers.\nChat is free and both players see it."
                % (self.n, DOCUMENT, self.penalty, self.penalty, self.n))

    def to_move(self):
        return [] if self.is_over() else [0, 1]

    def legal_actions(self, seat):
        return None             # free text: the answer is a phrase, not a token from a list

    def observation(self, seat):
        head = "DOCUMENT QUIZ. You are seat %d." % seat
        score = "Points: you %d, opponent %d." % (self.points[seat], self.points[1 - seat])
        rows = []
        for item in self.log:
            rows.append("  Q: %s\n     you answered %r (%+d), opponent answered %r (%+d)"
                        % (item["question"], item["answers"][seat], item["points"][seat],
                           item["answers"][1 - seat], item["points"][1 - seat]))
        history = "\n".join(rows) or "  (none yet)"
        if self.is_over():
            return "%s\nThe game is over. %s\nAnswers:\n%s" % (head, score, history)
        q, _expected = self.question()
        return ("%s\nQuestion %d of %d. %s\nEarlier questions:\n%s\n\nTHE DOCUMENT:\n%s\nQUESTION: %s\n"
                "Answer with a phrase from the document, or exactly \"unknown\"."
                % (head, self.round + 1, self.n, score, history, DOCUMENT, q))

    def normalize(self, seat, action):
        a = " ".join(str(action or "").split())
        a = a.strip(" .!\"'`")
        return a[:120] if a else None

    @staticmethod
    def _supported(answer, expected):
        a = " ".join(str(answer).lower().split())
        e = " ".join(str(expected).lower().split())
        if not e:
            return False
        if e in a:
            return True
        words = [w for w in e.replace(",", " ").split() if len(w) > 3]
        return bool(words) and all(w in a for w in words)

    def apply(self, actions):
        q, expected = self.question()
        answers = [str(actions.get(0, "")), str(actions.get(1, ""))]
        gained = []
        for seat in (0, 1):
            a = answers[seat]
            said_unknown = " ".join(a.lower().split()) == "unknown"
            if not expected:
                gained.append(1 if said_unknown else -self.penalty)
            elif said_unknown:
                gained.append(0)
            else:
                gained.append(2 if self._supported(a, expected) else -self.penalty)
        for seat in (0, 1):
            self.points[seat] += gained[seat]
        self.log.append({"question": q, "expected": expected or "(not in the document)",
                         "answers": answers, "points": gained})
        self.round += 1

    def is_over(self):
        return self.round >= self.n

    def result(self):
        p0, p1 = self.points
        winner = None if p0 == p1 else (0 if p0 > p1 else 1)
        return {"winner": winner if self.is_over() else None, "scores": [p0, p1],
                "reason": "points %d-%d after %d question(s)" % (p0, p1, len(self.log))}

    def frame(self):
        return {"questions": self.n, "round": self.round, "points": list(self.points),
                "log": [dict(x) for x in self.log], "over": self.is_over()}

    def random_action(self, seat, rng: random.Random):
        q, expected = self.question()
        pool = ["unknown", expected or "the keeper", "1847", "a guess"]
        return rng.choice([p for p in pool if p])
