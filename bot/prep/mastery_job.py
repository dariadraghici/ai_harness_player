"""Generic simulator self-play used during preparation.

This file runs only inside the sandbox child. It deliberately uses no bot
imports so generated simulators and their observations stay in the container.
"""

import json
import random
import time


MAX_STEPS = 160
MAX_GAMES = 96


def _tactical(sim, observation, variant, seat):
    """Find deterministic immediate wins and single-move blocks in the sample state."""
    try:
        state = sim.parse_observation(observation, variant, seat)
        if not isinstance(state, dict) or sim.current_player(state) != seat:
            return 0, 0
        legal = sim.legal_actions(state)
        wins, safe, threat = [], [], False
        for action in legal:
            child = sim.apply_action(state, action)
            if sim.is_terminal(child):
                values = sim.returns(child)
                if values[seat] > values[1 - seat]:
                    wins.append(action)
                continue
            if sim.current_player(child) != 1 - seat:
                continue
            threatened = False
            for reply in sim.legal_actions(child):
                after = sim.apply_action(child, reply)
                if sim.is_terminal(after):
                    values = sim.returns(after)
                    if values[1 - seat] > values[seat]:
                        threatened = True
                        break
            if threatened:
                threat = True
            else:
                safe.append(action)
        return len(wins), 1 if threat and len(safe) == 1 else 0
    except Exception:
        return 0, 0


def _key(state):
    if isinstance(state, dict):
        state = {k: v for k, v in state.items() if k != "seed"}
    return json.dumps(state, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def _value(sim, state, seat):
    try:
        values = sim.returns(state)
        return float(values[seat])
    except Exception:
        return 0.0


def _rollout(sim, state, seat, rng, limit=24):
    current = state
    for _ in range(limit):
        try:
            if sim.is_terminal(current):
                return _value(sim, current, seat)
            actions = sim.legal_actions(current)
            if not isinstance(actions, list) or not actions:
                return _value(sim, current, seat)
            current_player = sim.current_player(current)
            if current_player == -1:
                pair = [actions[rng.randrange(len(actions))], actions[rng.randrange(len(actions))]]
                if seat == 1:
                    pair.reverse()
                current = sim.apply_action(current, pair)
            else:
                current = sim.apply_action(current, actions[rng.randrange(len(actions))])
        except Exception:
            return 0.0
    return 0.0


def _choose(sim, state, seat, actions, policy, rng):
    if policy == "random" or len(actions) == 1:
        return actions[rng.randrange(len(actions))]
    best, best_value = actions[0], -float("inf")
    for action in actions:
        try:
            after = sim.apply_action(state, action)
            value = _value(sim, after, seat) if sim.is_terminal(after) else _rollout(
                sim, after, seat, rng, limit=10)
        except Exception:
            continue
        if value > best_value:
            best, best_value = action, value
    return best


def _play(sim, observation, variant, seat, ours, theirs, seed, deadline):
    try:
        state = sim.parse_observation(observation, variant, seat)
    except Exception:
        return {"error": "parse"}
    if not isinstance(state, dict):
        return {"error": "state"}
    rng = random.Random(seed)
    actions_seen = set()
    states_seen = set()
    first_action = ""
    repeated = 0
    steps = 0
    while steps < MAX_STEPS and time.monotonic() < deadline:
        try:
            if sim.is_terminal(state):
                break
            state_key = _key(state)
            if state_key in states_seen:
                return {"loop": True, "repeated": repeated, "steps": steps,
                        "first_action": first_action}
            states_seen.add(state_key)
            legal = sim.legal_actions(state)
            if not isinstance(legal, list) or not legal:
                return {"error": "legal", "steps": steps, "first_action": first_action}
            current = sim.current_player(state)
            if current == -1:
                a0 = _choose(sim, state, 0, legal, ours if seat == 0 else theirs, rng)
                a1 = _choose(sim, state, 1, legal, ours if seat == 1 else theirs, rng)
                action = [a0, a1]
                if seat == 1:
                    action = [a1, a0]
                if not all(a in legal for a in action):
                    return {"error": "illegal", "steps": steps, "first_action": first_action}
                state = sim.apply_action(state, action)
                observed = action[seat]
            else:
                policy = ours if current == seat else theirs
                observed = _choose(sim, state, current, legal, policy, rng)
                if observed not in legal:
                    return {"error": "illegal", "steps": steps, "first_action": first_action}
                state = sim.apply_action(state, observed)
            if not first_action:
                first_action = str(observed)
            token = str(observed)
            if token in actions_seen:
                repeated += 1
            actions_seen.add(token)
            steps += 1
        except Exception:
            return {"error": "apply", "steps": steps, "first_action": first_action}
    if steps >= MAX_STEPS or time.monotonic() >= deadline:
        return {"loop": True, "repeated": repeated, "steps": steps,
                "first_action": first_action}
    ours_value = _value(sim, state, seat)
    other_value = _value(sim, state, 1 - seat)
    return {"win": ours_value > other_value, "loss": ours_value < other_value,
            "draw": ours_value == other_value, "repeated": repeated, "steps": steps,
            "first_action": first_action}


def run(sim, payload):
    if sim is None:
        return {"ok": False, "reason": "no simulator"}
    observation = str(payload.get("observation") or "")
    variant = payload.get("variant") if isinstance(payload.get("variant"), dict) else {}
    budget = max(0.1, float(payload.get("budget_s") or 1.0))
    deadline = time.monotonic() + budget
    rng = random.Random(int(payload.get("seed") or 1))
    summary = {
        "games": 0, "wins": 0, "losses": 0, "draws": 0, "loops": 0,
        "illegal": 0, "errors": 0, "repeated_actions": 0, "steps": 0,
        "tactical_wins": 0, "tactical_blocks": 0, "seats": {"0": 0, "1": 0},
        "matchups": {},
    }
    tactical_wins, tactical_blocks = _tactical(sim, observation, variant, 0)
    summary["tactical_wins"] = tactical_wins
    summary["tactical_blocks"] = tactical_blocks
    matchups = (("greedy", "random"), ("random", "greedy"), ("greedy", "greedy"))
    while summary["games"] < MAX_GAMES and time.monotonic() < deadline:
        ours, theirs = matchups[summary["games"] % len(matchups)]
        seat = summary["games"] % 2
        result = _play(sim, observation, variant, seat, ours, theirs,
                       rng.randrange(2 ** 31), deadline)
        summary["games"] += 1
        summary["seats"][str(seat)] += 1
        summary["steps"] += int(result.get("steps", 0))
        summary["repeated_actions"] += int(result.get("repeated", 0))
        key = "%s_vs_%s" % (ours, theirs)
        summary["matchups"][key] = summary["matchups"].get(key, 0) + 1
        if result.get("error"):
            if result["error"] == "illegal":
                summary["illegal"] += 1
            else:
                summary["errors"] += 1
        elif result.get("loop"):
            summary["loops"] += 1
        elif result.get("win"):
            summary["wins"] += 1
        elif result.get("loss"):
            summary["losses"] += 1
        else:
            summary["draws"] += 1
    summary["elapsed_s"] = round(budget - max(0.0, deadline - time.monotonic()), 3)
    return {"ok": True, "summary": summary}
