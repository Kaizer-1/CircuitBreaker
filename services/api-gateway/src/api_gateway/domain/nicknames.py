"""The nickname blocklist (A-01, projector safety): offensive nicknames are refused at join.

Nicknames are shown on the projector and the leaderboard, so a short list of offensive words in
English and Hindi/Hinglish is kept in a data file next to this module (nickname_blocklist.txt),
not in code. A nickname and every list entry go through the same normalisation, then a blocked
word anywhere inside the nickname refuses it:

1. lowercase;
2. leetspeak mapped: 0→o, 1→i, 3→e, 4→a, @→a, $→s, 5→s, 7→t;
3. underscores and the remaining digits removed (f_u_c_k, x99);
4. repeated letters collapsed (fuuuck → fuck, gaaandu → gandu).

Plain substring matching would also refuse ordinary names that happen to contain a short blocked
word (Harshit, Nigam, Nazim, Randip). The file's [allowed] section lists such words and name
fragments. A blocked word is excused only where it lies entirely inside an allowed word in the
nickname: "Harshit" passes (its "shit" is inside "rshit"), "Harshit_Shit" does not (the second
"shit" is inside nothing allowed). Nothing is removed or joined, so one exception can never
create or hide another match.
"""

import re
from collections.abc import Iterable
from importlib import resources

_LEET = str.maketrans(
    {"0": "o", "1": "i", "3": "e", "4": "a", "@": "a", "$": "s", "5": "s", "7": "t"}
)
_DROP = re.compile(r"[_0-9]")
_REPEATS = re.compile(r"(.)\1+")


def normalise(text: str) -> str:
    return _REPEATS.sub(r"\1", _DROP.sub("", text.lower().translate(_LEET)))


class NicknameBlocklist:
    def __init__(self, blocked: Iterable[str], allowed: Iterable[str] = ()) -> None:
        self.blocked = sorted({normalise(w) for w in blocked if w.strip()})
        self.allowed = sorted({normalise(w) for w in allowed if w.strip()})

    @classmethod
    def from_text(cls, text: str) -> "NicknameBlocklist":
        sections: dict[str, list[str]] = {"blocked": [], "allowed": []}
        current: list[str] | None = None
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("[") and line.endswith("]"):
                current = sections[line[1:-1]]
                continue
            if current is None:
                raise ValueError(f"entry outside a section: {line!r}")
            current.append(line)
        return cls(sections["blocked"], sections["allowed"])

    @classmethod
    def default(cls) -> "NicknameBlocklist":
        """The list shipped with the gateway (src/api_gateway/domain/nickname_blocklist.txt)."""
        text = (resources.files("api_gateway") / "domain" / "nickname_blocklist.txt").read_text()
        return cls.from_text(text)

    def refuses(self, nickname: str) -> bool:
        text = normalise(nickname)
        excused = [(at, at + len(w)) for w in self.allowed for at in _occurrences(text, w)]
        return any(
            not any(a <= at and at + len(word) <= b for a, b in excused)
            for word in self.blocked
            for at in _occurrences(text, word)
        )


def _occurrences(text: str, word: str) -> list[int]:
    """Every position where `word` starts in `text`, overlapping ones included."""
    found, at = [], text.find(word)
    while at != -1:
        found.append(at)
        at = text.find(word, at + 1)
    return found
