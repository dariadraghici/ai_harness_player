"""Game family and game profile from the rules text.

Owner: luckydevs-3. Status: stub with a simple LLM version (one call per variant).

Done means: a robust profile (family, information, move order, speech
restrictions, exact action format, a valid example action) and a playbook,
cross-checked against sample_turn when present (e.g. the example action
should look like the sample's legal actions).
"""

import json
import re

from .. import config, llm, log
from ..chat.decide import fill, parse_json_object, template
from ..interfaces import GameProfile


def classify_variant(game_name, label, rules, sample_turn, timeout, thinking=True):
    """Return (GameProfile, playbook_text). Never raises; defaults on failure."""
    sample = ""
    if isinstance(sample_turn, dict):
        s = {k: sample_turn.get(k) for k in ("observation", "legal_actions", "turn")}
        sample = "SAMPLE TURN (one real turn message from a sample game):\n" + json.dumps(
            s, ensure_ascii=True)[:4000]
    msgs = [{"role": "system", "content": template("classify_system.txt")},
            {"role": "user", "content": fill(template("classify_user.txt"), GAME_NAME=game_name,
                                             LABEL=label, RULES=rules, SAMPLE=sample)}]
    text = llm.complete(msgs, max_tokens=config.PREP_MAX_TOKENS if thinking else 1500,
                        timeout=timeout,
                        thinking_budget=config.PREP_THINKING_BUDGET if thinking else None,
                        tag="classify")
    obj = parse_json_object(text)
    if not obj:
        log.warning("classify %s/%s: no usable answer, default profile" % (game_name, label))
        return _default_profile(sample_turn, rules), ""
    profile = GameProfile.from_dict(obj)
    if not profile.example_action:
        profile.example_action = _default_profile(sample_turn, rules).example_action
    playbook = obj.get("playbook") or ""
    if isinstance(playbook, list):
        playbook = "\n".join(str(x) for x in playbook)
    return profile, str(playbook)


_EXAMPLE_RE = re.compile(
    r"(?:exactly like|for example|for instance|e\.g\.|such as|example:|like)\s*:?\s*\"([^\"\n]{1,60})\"",
    re.IGNORECASE)


def example_from_rules(rules):
    """Generic heuristic: the first quoted example action in the rules text, or ""."""
    text = str(rules or "")
    idx = text.lower().find("action format")
    for chunk in ((text[idx:] if idx >= 0 else ""), text):
        m = _EXAMPLE_RE.search(chunk)
        if m:
            return m.group(1).strip()
    return ""


def _default_profile(sample_turn, rules=""):
    p = GameProfile()
    p.example_action = example_from_rules(rules)
    if isinstance(sample_turn, dict) and isinstance(sample_turn.get("legal_actions"), list) \
            and sample_turn["legal_actions"]:
        p.example_action = str(sample_turn["legal_actions"][0])
    return p
