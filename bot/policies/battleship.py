"""Probability-density Battleship targeting for a 6x6 board."""

import itertools
import re
import time

from .. import memory

STRATEGY_FILE = "battleship.json"
SIZE = 6
FLEET = (3, 2, 2)


def can_handle(rules_text, sample_turn=None):
    text = str(rules_text or "").casefold()
    return 0.98 if "battleship" in text and re.search(r"\bfire\s+[a-f][1-6]\b", text) else 0.0


def _cell(token):
    match = re.fullmatch(r"([a-f])([1-6])", str(token or "").casefold())
    return (ord(match.group(1)) - 97, int(match.group(2)) - 1) if match else None


def _token(cell):
    return "%s%d" % (chr(97 + cell[0]), cell[1] + 1)


def parse_state(observation):
    text = str(observation or "")
    fired = {_cell(token) for token in re.findall(r"\b[a-f][1-6]\b", text, re.I)}
    fired.discard(None)
    misses = set()
    hits = set()
    for match in re.finditer(r"\b(miss|hit|sunk)\b[^a-f1-6]{0,20}([a-f][1-6])", text, re.I):
        cell = _cell(match.group(2))
        fired.add(cell)
        if match.group(1).casefold() == "miss":
            misses.add(cell)
        else:
            hits.add(cell)
    for match in re.finditer(r"\b([a-f][1-6])\s*[:=-]\s*(miss|hit|sunk)\b", text, re.I):
        cell = _cell(match.group(1))
        fired.add(cell)
        if match.group(2).casefold() == "miss":
            misses.add(cell)
        else:
            hits.add(cell)
    sunk = 0
    sunk_match = re.search(r"\bsunk\s+([0-3])", text, re.I)
    if sunk_match:
        sunk = int(sunk_match.group(1))
    return fired, misses, hits, sunk


def _placements(length, blocked):
    for row in range(SIZE):
        for col in range(SIZE - length + 1):
            cells = frozenset((col + offset, row) for offset in range(length))
            if not cells & blocked:
                yield cells
    for col in range(SIZE):
        for row in range(SIZE - length + 1):
            cells = frozenset((col, row + offset) for offset in range(length))
            if not cells & blocked:
                yield cells


def _density(fired, misses, hits, sunk):
    remaining = FLEET[sunk:]
    placements = []
    for length in remaining:
        choices = [cells for cells in _placements(length, misses) if not hits or cells & hits]
        placements.append(choices)
    density = {}
    for combination in itertools.product(*placements):
        if len(set(combination)) != len(combination):
            continue
        covered = set().union(*combination)
        if hits - covered:
            continue
        for cell in covered:
            if cell not in fired:
                density[cell] = density.get(cell, 0) + 1
    return density


def prep(ctx):
    try:
        memory.write_json_atomic(
            memory.variant_dir(ctx["game_id"], ctx["rules_hash"]) + "/" + STRATEGY_FILE,
            {"version": 1, "size": SIZE, "fleet": list(FLEET)})
    except Exception:
        pass
    return {"size": SIZE, "fleet": list(FLEET)}


def decide(state, deadline):
    turn = state.get("turn") or state
    fired, misses, hits, sunk = parse_state(turn.get("observation", ""))
    density = _density(fired, misses, hits, sunk)
    if not density:
        density = {cell: 1 for cell in itertools.product(range(SIZE), repeat=2)
                   if cell not in fired}
    adjacent = set()
    for col, row in hits:
        adjacent.update((col + dc, row + dr) for dc, dr in
                        ((1, 0), (-1, 0), (0, 1), (0, -1)))
    available = [cell for cell in density if cell not in fired]
    best = max(available, key=lambda cell: (
        density[cell] + (density[cell] * 0.25 if cell in adjacent else 0),
        -((cell[0] + cell[1]) % 2), -abs(cell[0] - 2.5) - abs(cell[1] - 2.5), cell))
    return [{"action": "fire " + _token(best), "score": 1.0}]
