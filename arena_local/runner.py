"""Plays one practice match following docs/ARENA-CONTRACT.md section 2.

This is OUR emulator (no official practice arena was provided). It follows
the contract as written; where the contract is silent we chose simple rules:
  - a match is exactly `games` games (best_of = games), seats swap every game;
  - the variant is drawn at random per game (or fixed with --variant);
  - with prep: one container per player gets prep and then plays the whole
    match warm (30 s study clock); without prep: cold, 120 s study clock.
"""

import json
import random
import threading
import time

PREP_CLOCK, WARM_STUDY, COLD_STUDY = 600, 30, 120


class PlayerStats:
    def __init__(self, name):
        self.name = name
        self.warnings = 0
        self.timeouts = 0
        self.bad = 0
        self.crashes = 0
        self.lost_by = []
        self.max_answer = 0.0
        self.sum_answer = 0.0
        self.n_answers = 0
        self.prep_ready_s = None

    def time(self, dt):
        self.max_answer = max(self.max_answer, dt)
        self.sum_answer += dt
        self.n_answers += 1

    def to_dict(self):
        d = dict(self.__dict__)
        d["avg_answer"] = round(self.sum_answer / self.n_answers, 3) if self.n_answers else 0.0
        d["max_answer"] = round(self.max_answer, 3)
        d.pop("sum_answer")
        return d


def _sample_turn(game, variant, seed):
    """One real turn message from a quick random sample game of this variant."""
    rng = random.Random(seed)
    s = game.new(variant, rng)
    for _ in range(1 + rng.randint(0, 2)):
        if game.over(s):
            break
        acts = {seat: game.random_action(s, seat, rng) for seat in game.to_move(s)}
        game.apply(s, acts)
        if game.over(s):
            s = game.new(variant, rng)
            break
    seat = game.to_move(s)[0]
    legal = game.legal(s, seat)
    return {"type": "turn", "game_index": 1, "turn": 2, "observation": game.observation(s, seat),
            "legal_actions": legal if legal is not None and len(legal) <= 50 else None,
            "seconds": 20, "chat": []}


def _chat_view(chat, seat):
    """The match chat as one player sees it: "from" is "you" or "opponent"."""
    out = []
    for e in chat:
        v = {k: val for k, val in e.items() if k != "seat"}
        v["from"] = "you" if e["seat"] == seat else "opponent"
        out.append(v)
    return out


def play_match(game, players, games=2, prep=False, llm=None, variant_label=None, seed=None,
               move_seconds=20, log=print):
    rng = random.Random(seed)
    llm = llm or {}
    stats = [PlayerStats(p.name) for p in players]
    replay = {"game_id": game.game_id, "prep": prep, "games": [], "chat": []}
    chat = []    # entries with an absolute "seat" key; rendered per player
    match_id = "local-%d" % int(time.time())
    team = [p.name for p in players]

    # ---------------------------------------------------------------- prep
    if prep:
        variants = []
        for i, v in enumerate(game.variants):
            variants.append({"label": v["label"], "rules": v["rules"],
                             "sample_turn": _sample_turn(game, v, (seed or 0) + i)})
        threads, results = [], [None, None]
        for i, p in enumerate(players):
            msg = {"type": "prep", "game_id": game.game_id, "game_name": game.game_name,
                   "variants": variants, "seconds": PREP_CLOCK, "you": team[i],
                   "study_seconds": WARM_STUDY, "move_seconds": move_seconds, "llm": llm}

            def run(i=i, p=p, msg=msg):
                results[i] = p.ask(msg, PREP_CLOCK)
            th = threading.Thread(target=run)
            th.start()
            threads.append(th)
        for th in threads:
            th.join()
        for i, (status, obj, dt) in enumerate(results):
            stats[i].prep_ready_s = round(dt, 2)
            ok = status == "ok" and obj.get("type") == "ready"
            log("prep %s: %s in %.1fs" % (players[i].name, "ready" if ok else status, dt))
            if not ok:
                stats[i].lost_by.append("prep:" + status)

    score = [0, 0]   # match score per player index
    for g in range(1, games + 1):
        variant = next((v for v in game.variants if v["label"] == variant_label), None) \
            if variant_label else rng.choice(game.variants)
        variant = variant or game.variants[0]
        seat_of = [0, 1] if g % 2 == 1 else [1, 0]          # player index -> seat
        player_at = {seat_of[i]: i for i in range(2)}
        state = game.new(variant, rng)
        for i, p in enumerate(players):
            if hasattr(p, "bind"):
                p.bind(state, seat_of[i])
        study = WARM_STUDY if prep else COLD_STUDY
        lost = {}       # player index -> reason
        ginfo = {"game_index": g, "variant": variant["label"], "seats": seat_of, "moves": []}
        log("game %d: variant %s, seats %s" % (g, variant["label"], seat_of))

        # ------------------------------------------------------------ start
        def start_msg(i):
            return {"type": "start", "match_id": match_id, "game_index": g, "best_of": games,
                    "game_id": game.game_id, "game_name": game.game_name,
                    "variant": {"label": variant["label"], "settings": variant.get("settings", {})},
                    "rules": variant["rules"], "seat": seat_of[i], "you": team[i],
                    "opponent": team[1 - i],
                    "match_score": {"you": score[i], "opponent": score[1 - i]},
                    "study_seconds": study, "move_seconds": move_seconds,
                    "chat": _chat_view(chat, seat_of[i]), "llm": llm}
        res = [None, None]
        ths = []
        for i, p in enumerate(players):
            def run(i=i, p=p, m=start_msg(i)):
                res[i] = p.ask(m, study)
            th = threading.Thread(target=run)
            th.start()
            ths.append(th)
        for th in ths:
            th.join()
        for i, (status, obj, dt) in enumerate(res):
            stats[i].time(dt)
            if status != "ok" or obj.get("type") != "ready":
                lost[i] = "start:" + status
                stats[i].timeouts += status == "timeout"
                stats[i].crashes += status == "crash"
                continue
            if isinstance(obj.get("chat"), str):
                chat.append({"seat": seat_of[i], "game": g, "game_name": game.game_name,
                             "phase": "study", "turn": 0, "kind": "chat",
                             "text": obj["chat"][:200]})

        # ------------------------------------------------------------ turns
        turn_no = 0
        warnings = [0, 0]
        while not lost and not game.over(state):
            turn_no += 1
            movers = [player_at[s] for s in game.to_move(state)]
            answers = {}

            def ask_one(i, warning=None):
                seat = seat_of[i]
                legal = game.legal(state, seat)
                msg = {"type": "turn", "game_index": g, "turn": turn_no,
                       "observation": game.observation(state, seat),
                       "legal_actions": legal if legal is not None and len(legal) <= 50 else None,
                       "seconds": move_seconds, "chat": _chat_view(chat, seat)}
                if warning:
                    msg["warning"] = warning
                return players[i].ask(msg, move_seconds)

            def play(i):
                warning = None
                while True:
                    status, obj, dt = ask_one(i, warning)
                    stats[i].time(dt)
                    if status in ("timeout", "crash"):
                        stats[i].timeouts += status == "timeout"
                        stats[i].crashes += status == "crash"
                        lost[i] = status
                        return
                    reason = None
                    if status == "bad":
                        reason = "invalid JSON"
                    elif obj.get("type") != "action":
                        reason = "wrong type"
                    elif not isinstance(obj.get("action"), str) or not obj["action"]:
                        reason = "missing or empty action"
                    else:
                        ok, why = game.check(state, seat_of[i], obj["action"])
                        if not ok:
                            reason = "illegal action: " + why
                    if reason is None:
                        answers[i] = obj
                        return
                    warnings[i] += 1
                    stats[i].warnings += 1
                    stats[i].bad += 1
                    log("  warning for %s: %s (%r)" % (players[i].name, reason, obj))
                    if warnings[i] >= 2:
                        lost[i] = "second bad answer"
                        return
                    warning = {"count": warnings[i], "max": 2, "reason": reason,
                               "rejected": obj.get("action") if isinstance(obj, dict) else obj}

            ths = [threading.Thread(target=play, args=(i,)) for i in movers]
            for th in ths:
                th.start()
            for th in ths:
                th.join()
            if lost:
                break
            for i in movers:
                c = answers[i].get("chat")
                if isinstance(c, str):
                    chat.append({"seat": seat_of[i], "game": g, "game_name": game.game_name,
                                 "phase": "play", "turn": turn_no, "kind": "chat", "text": c[:200]})
            acts = {seat_of[i]: answers[i]["action"] for i in movers}
            ginfo["moves"].append({"turn": turn_no, "actions": {players[i].name: answers[i]["action"]
                                                                for i in movers}})
            game.apply(state, acts)

        # ------------------------------------------------------------ end
        if lost:
            loser = next(iter(lost))
            result = {loser: "loss", 1 - loser: "win"} if len(lost) == 1 else {0: "draw", 1: "draw"}
            reason = "; ".join("%s lost: %s" % (players[i].name, r) for i, r in lost.items())
            pts = [0, 0]
            for i in lost:
                stats[i].lost_by.append(lost[i])
        else:
            sc = game.scores(state)
            pts = [sc[seat_of[0]], sc[seat_of[1]]]
            result = {0: "win", 1: "loss"} if pts[0] > pts[1] else {0: "loss", 1: "win"} \
                if pts[1] > pts[0] else {0: "draw", 1: "draw"}
            reason = "game over"
        for i in range(2):
            if result[i] == "win":
                score[i] += 1
        for i, p in enumerate(players):
            p.send({"type": "end", "game_index": g, "result": result[i], "reason": reason,
                    "scores": {"you": pts[i], "opponent": pts[1 - i]},
                    "match_score": {"you": score[i], "opponent": score[1 - i]},
                    "final_observation": game.observation(state, seat_of[i]),
                    "match_over": g == games, "chat": _chat_view(chat, seat_of[i])})
        ginfo.update({"result": {players[i].name: result[i] for i in range(2)}, "reason": reason,
                      "points": {players[i].name: pts[i] for i in range(2)}})
        replay["games"].append(ginfo)
        log("game %d: %s (%s), points %s" % (g, ginfo["result"], reason, ginfo["points"]))
        time.sleep(0.2)

    # ---------------------------------------------------------------- shutdown
    for i, p in enumerate(players):
        code, dt = p.close()
        replay.setdefault("exit", {})[p.name] = {"code": code, "seconds": round(dt, 2)}
    replay["chat"] = chat
    replay["match_score"] = {players[i].name: score[i] for i in range(2)}
    replay["stats"] = {p.name: dict(stats[i].to_dict(), stray_stdout=p.stray)
                       for i, p in enumerate(players)}
    return replay


def summary_ok(replay, name):
    """True when the named player had no warning, timeout, crash, stray line and exited cleanly."""
    st = replay["stats"][name]
    ex = replay.get("exit", {}).get(name, {})
    return (st["warnings"] == 0 and st["timeouts"] == 0 and st["crashes"] == 0
            and not st["stray_stdout"] and not st["lost_by"] and ex.get("code") == 0)


def dump(replay, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(replay, f, indent=1, ensure_ascii=True)
