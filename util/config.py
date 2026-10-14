"""config: tiny JSON config loader shared by all mactools daemons.

File: ~/.config/mactools/<name>.json. Unknown keys are reported, missing keys take the
defaults, wrong-typed values are rejected with the key named — a daemon must never
silently run with a half-parsed config.

Hotkey strings look like "opt+left", "opt+shift+=", "ctrl+cmd+right". Modifier names:
shift ctrl/control opt/option/alt cmd/command. Key names: left right up down tab esc
return space minus/- equal/= plus/+ a-z 0-9 f1-f20 missioncontrol.
"""

import json
import os

from macbridge import FLAG_NAMES, MODIFIER_MASK  # noqa: E402

CONFIG_DIR = os.path.expanduser("~/.config/mactools")

KEYCODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11,
    "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21,
    "6": 22, "5": 23, "=": 24, "equal": 24, "plus": 24, "+": 24, "9": 25, "7": 26, "-": 27,
    "minus": 27, "_": 27, "8": 28, "0": 29, "]": 30, "o": 31, "u": 32, "[": 33, "i": 34, "p": 35,
    "return": 36, "enter": 36, "l": 37, "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43,
    "/": 44, "n": 45, "m": 46, ".": 47, "tab": 48, "space": 49, "`": 50, "backtick": 50,
    "delete": 51, "backspace": 51, "esc": 53, "escape": 53, "f17": 64, "f18": 79, "f19": 80,
    "f20": 90, "f5": 96, "f6": 97, "f7": 98, "f3": 99, "f8": 100, "f9": 101, "f11": 103,
    "f13": 105, "f16": 106, "f14": 107, "f10": 109, "f12": 111, "f15": 113, "home": 115,
    "pageup": 116, "f4": 118, "end": 119, "f2": 120, "pagedown": 121, "f1": 122, "left": 123,
    "right": 124, "down": 125, "up": 126, "missioncontrol": 160, "padplus": 69, "padminus": 78,
}


class ConfigError(Exception):
    pass


def parse_hotkey(spec):
    """'opt+shift+=' -> (keycode, flags). Raises ConfigError on unknown names.

    The key itself may be '+' or '-': write it last, e.g. 'opt+shift++' / 'opt+-'.
    """
    if not isinstance(spec, str) or not spec.strip():
        raise ConfigError(f"hotkey must be a non-empty string, got {spec!r}")
    s = spec.strip()
    parts = [p.strip().lower() for p in s.split("+")]
    if parts and parts[-1] == "" and len(parts) >= 2 and parts[-2] != "":
        raise ConfigError(f"hotkey {spec!r}: trailing '+' with no key (for the plus key itself write '...++')")
    if "" in parts[:-1] and not (s.endswith("++") and parts[:-2].count("") == 0):
        raise ConfigError(f"hotkey {spec!r}: empty token (double '+' is only valid at the very end, meaning the plus key)")
    tokens = [p for p in parts if p]
    if s.endswith("++"):
        tokens.append("+")
    if not tokens:
        raise ConfigError(f"hotkey {spec!r}: empty")
    flags, key = 0, None
    for p in tokens:
        if p in FLAG_NAMES:
            flags |= FLAG_NAMES[p]
        elif p in KEYCODES:
            if key is not None:
                raise ConfigError(f"hotkey {spec!r}: two keys ({key!r} and {p!r})")
            key = p
        else:
            raise ConfigError(f"hotkey {spec!r}: unknown token {p!r}")
    if key is None:
        raise ConfigError(f"hotkey {spec!r}: no key, only modifiers")
    return KEYCODES[key], flags & MODIFIER_MASK


def load(name, defaults):
    """Return a dict: defaults overlaid with ~/.config/mactools/<name>.json. Validates types
    against the defaults (None in defaults = any type allowed)."""
    path = os.path.join(CONFIG_DIR, f"{name}.json")
    cfg = dict(defaults)
    if not os.path.exists(path):
        return cfg, path
    try:
        with open(path, "r", encoding="utf-8") as fh:
            user = json.load(fh)
    except (OSError, ValueError) as e:
        raise ConfigError(f"{path}: {e}") from e
    if not isinstance(user, dict):
        raise ConfigError(f"{path}: top level must be an object")
    user = {k: v for k, v in user.items() if not k.startswith("_")}  # "_comment" and friends
    unknown = sorted(set(user) - set(defaults))
    if unknown:
        raise ConfigError(f"{path}: unknown keys {unknown}; valid keys: {sorted(defaults)}")
    for k, v in user.items():
        d = defaults[k]
        if d is not None and type(v) is not type(d) and not (isinstance(d, float) and isinstance(v, int)):
            raise ConfigError(f"{path}: {k!r} must be {type(d).__name__}, got {type(v).__name__}")
        cfg[k] = v
    return cfg, path


def write_defaults(name, defaults, comment=None):
    """Create the config file with defaults if it does not exist. Returns the path."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    path = os.path.join(CONFIG_DIR, f"{name}.json")
    if not os.path.exists(path):
        body = dict(defaults)
        if comment:
            body = {"_comment": comment, **body}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(body, fh, indent=2)
            fh.write("\n")
    return path
