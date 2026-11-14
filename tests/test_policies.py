import time
import unittest

from bot.interfaces import GameProfile, VariantSpec
from bot.policies import allocation


class AllocationPolicyTests(unittest.TestCase):

    def setUp(self):
        rules = ("Split 60 troops across 6 battlefields. Field worths are "
                 "5 2 3 1 4 6. Allocations must sum to 60.")
        self.spec = VariantSpec("blotto", "Colonel Blotto", "standard", rules, "hash",
                                profile=GameProfile(family="read_opponent"))

    def test_candidate_has_exact_total_and_avoids_reported_bad_shape(self):
        turn = {"observation": "Field worths: 5 2 3 1 4 6", "variant": {}}
        rows = allocation.decide({"spec": self.spec, "turn": turn},
                                 time.monotonic() + 1.0)
        self.assertTrue(rows)
        for row in rows:
            values = tuple(int(value) for value in row["action"].split())
            self.assertEqual(len(values), 6)
            self.assertEqual(sum(values), 60)
            self.assertTrue(all(value >= 0 for value in values))
            self.assertNotEqual(values, (0, 20, 0, 40, 0, 0))

    def test_strict_worth_checker(self):
        self.assertEqual(allocation.round_winner((5, 0), (0, 5), (5, 2)), 1)
        self.assertEqual(allocation.round_winner((0, 5), (5, 0), (5, 2)), -1)
        self.assertEqual(allocation.round_winner((5, 5), (5, 5), (5, 2)), 0)

    def test_router_prefers_allocation_rules(self):
        from bot.policies import choose
        self.assertEqual(choose(self.spec.rules).__name__, "bot.policies.allocation")
