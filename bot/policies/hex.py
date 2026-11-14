"""Time-boxed Hex policy with immediate tactics and shortest-path ordering."""

import re
import time

from .. import memory

STRATEGY_FILE = "hex.json"
SIZE = 7
NEIGHBORS = ((-1, 0), (1, 0), (0, -1), (1, -1), (-1, 1), (0, 1))


def can_handle(rules_text, sample_turn=None):
    text = str(rules_text or "").casefold()
    return 0.98 if "hex" in text and "place" in text else 0.0


def _cell(token):
    match = re.fullmatch(r"([a-g])([1-7])", str(token or "").casefold())
    return (ord(match.group(1)) - 97, int(match.group(2)) - 1) if match else None


def _token(cell):
    return "%s%d" % (chr(97 + cell[0]), cell[1] + 1)


def parse_board(observation):
    board = [[None] * SIZE for _ in range(SIZE)]
    for row, line in enumerate(str(observation or "").splitlines()):
        cells = [value for value in line.upper() if value in ".XO-"]
        if len(cells) >= SIZE:
            for col, value in enumerate(cells[-SIZE:]):
                board[row][col] = None if value in ".-" else value.upper()
    for token, value in re.findall(r"\b([a-g][1-7])\s*[:=]\s*([XO])\b",
                                   str(observation or ""), re.I):
        cell = _cell(token)
        board[cell[1]][cell[0]] = value.upper()
    for value, token in re.findall(r"\b([XO])\s+(?:at|place)\s+([a-g][1-7])\b",
                                   str(observation or ""), re.I):
        cell = _cell(token)
        board[cell[1]][cell[0]] = value.upper()
    return board


def _winner(board, player):
    starts = [(0, col) for col in range(SIZE)] if player == "X" else [(row, 0) for row in range(SIZE)]
    target = (lambda row, col: row == SIZE - 1) if player == "X" else (lambda row, col: col == SIZE - 1)
    stack = list(starts)
    seen = set()
    while stack:
        row, col = stack.pop()
        if (row, col) in seen or board[row][col] != player:
            continue
        seen.add((row, col))
        if target(row, col):
            return True
        for dr, dc in NEIGHBORS:
            nr, nc = row + dr, col + dc
            if 0 <= nr < SIZE and 0 <= nc < SIZE:
                stack.append((nr, nc))
    return False


def _path_score(board, player, move):
    row, col = move
    score = 0
    for dr, dc in NEIGHBORS:
        nr, nc = row + dr, col + dc
        if 0 <= nr < SIZE and 0 <= nc < SIZE and board[nr][nc] == player:
            score += 2
    score -= abs(row - 3) + abs(col - 3)
    return score


def prep(ctx):
    try:
        memory.write_json_atomic(
            memory.variant_dir(ctx["game_id"], ctx["rules_hash"]) + "/" + STRATEGY_FILE,
            {"version": 1, "size": SIZE, "opening": "d4"})
    except Exception:
        pass
    return {"size": SIZE, "opening": "d4"}


def decide(state, deadline):
    turn = state.get("turn") or state
    board = parse_board(turn.get("observation", ""))
    empty = [(row, col) for row in range(SIZE) for col in range(SIZE) if board[row][col] is None]
    if not empty:
        return []
    for player in ("X", "O"):
        for cell in empty:
            board[cell[0]][cell[1]] = player
            won = _winner(board, player)
            board[cell[0]][cell[1]] = None
            if won:
                return [{"action": "place " + _token((cell[1], cell[0])), "score": 1.0}]
    if time.monotonic() >= deadline:
        cell = empty[0]
    else:
        cell = max(empty, key=lambda item: _path_score(board, "X", item))
    return [{"action": "place " + _token((cell[1], cell[0])), "score": 0.7}]
