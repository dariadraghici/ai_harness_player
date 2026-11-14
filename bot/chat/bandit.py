"""Thompson sampling over influence tactics (PROMPT-CHAT section 11.3).

Owner: luckydevs-2 area. Status: real. Standard library only (random.betavariate).

One arm per tactic of a family, plus the "quiet" arm whose reward statistics are the CONTROLS of that family: a tactic
only wins turns when it beats doing nothing. The reward is the scored `hit`. The prior of an arm is the family-wide
statistics of ALL opponents (capped at PRIOR_CAP pseudo-observations, so one opponent's own data wins quickly) and the
opponent's own counts are added on top. The statistics live in /memory (opponents.py), so every container of ours
learns from the round robin and from earlier matches.

Exploration is bounded: a draw that disagrees with the greedy (highest posterior mean) arm is an exploration pick; it
is allowed at most EXPLORE_MAX times per match and never when the caller says the turn is decisive (a large gap in a
game that matters). Then the greedy arm is played.
"""

PRIOR_CAP = 6.0          # pseudo-observations the family-wide statistics may contribute
EXPLORE_MAX = 4          # exploration picks per match


def posterior(own, glob):
    """(alpha, beta) of an arm from the opponent's own {scored, hits} and the family-wide {scored, hits}."""
    own_s, own_h = max(0, int((own or {}).get("scored", 0))), max(0, int((own or {}).get("hits", 0)))
    g_s, g_h = max(0, int((glob or {}).get("scored", 0))), max(0, int((glob or {}).get("hits", 0)))
    f = min(1.0, PRIOR_CAP / float(g_s)) if g_s else 0.0
    return 1.0 + own_h + f * g_h, 1.0 + max(0, own_s - own_h) + f * max(0, g_s - g_h)


def stat_key(arm):
    return "control" if arm == "quiet" else arm


class Bandit:
    def __init__(self, own=None, glob=None, explored=0):
        self.own = own or {}               # {arm key: {"scored", "hits"}} for this opponent and family
        self.glob = glob or {}             # the same over all opponents
        self.explored = int(explored)

    def params(self, arm):
        k = stat_key(arm)
        return posterior(self.own.get(k), self.glob.get(k))

    def mean(self, arm):
        a, b = self.params(arm)
        return a / (a + b)

    def choose(self, arms, rng, greedy=False, may_explore=True):
        """(arm, info). info: {"theta": {arm: draw}, "mean": {arm: mean}, "explore": bool, "greedy": arm}."""
        arms = list(arms)
        means = {a: self.mean(a) for a in arms}
        best_mean = max(arms, key=lambda a: (means[a], -arms.index(a)))
        info = {"mean": {a: round(m, 3) for a, m in means.items()}, "greedy": best_mean, "explore": False,
                "theta": {}}
        if greedy or rng is None or len(arms) == 1:
            return best_mean, info
        draws = {}
        for a in arms:
            al, be = self.params(a)
            draws[a] = rng.betavariate(al, be)
        info["theta"] = {a: round(v, 3) for a, v in draws.items()}
        pick = max(arms, key=lambda a: (draws[a], -arms.index(a)))
        if pick != best_mean:
            if not may_explore or self.explored >= EXPLORE_MAX:
                return best_mean, info
            info["explore"] = True
        return pick, info
