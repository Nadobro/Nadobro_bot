"""A MarkdownV2 entity validator, for use as a test oracle.

Telegram rejects a message with ``Can't parse entities`` when a reserved
character appears unescaped outside an entity, or when an entity delimiter is
left unbalanced. ``handlers/callbacks.py`` carries a rescue path for exactly
that failure, which means it happens in production: the user watches a card
collapse into raw text with visible backslashes.

This module reproduces Telegram's rules closely enough to catch that bug class
before a card ships. It is deliberately conservative — it reports a problem only
where Telegram definitely rejects, so a passing card here is not *proven* good,
but a failing one is genuinely broken.

Reference: https://core.telegram.org/bots/api#markdownv2-style
"""

from __future__ import annotations

from dataclasses import dataclass

# Characters Telegram requires to be escaped in ordinary MarkdownV2 text.
RESERVED = set(r"_*[]()~`>#+-=|{}.!")

# Inside a code span only the backtick and backslash are special.
CODE_RESERVED = set("`\\")


@dataclass(frozen=True)
class Md2Problem:
    index: int
    char: str
    reason: str
    excerpt: str

    def __str__(self) -> str:  # pragma: no cover - diagnostics only
        return f"col {self.index}: {self.reason} ({self.char!r}) near …{self.excerpt}…"


def _excerpt(text: str, i: int, width: int = 28) -> str:
    lo = max(0, i - width // 2)
    return text[lo : lo + width].replace("\n", "⏎")


def find_problems(text: str) -> list[Md2Problem]:
    """Return every position where Telegram would reject this MarkdownV2 text."""
    problems: list[Md2Problem] = []
    i = 0
    n = len(text)

    # Entity delimiters we track for balance. Telegram treats these as toggles.
    open_delims: list[tuple[str, int]] = []

    while i < n:
        ch = text[i]

        # 1. Backslash escape consumes the next character, whatever it is.
        if ch == "\\":
            i += 2
            continue

        # 2. Pre block ```...``` — everything inside is literal except \ and `.
        if text.startswith("```", i):
            end = text.find("```", i + 3)
            if end == -1:
                problems.append(Md2Problem(i, "```", "unclosed pre block", _excerpt(text, i)))
                break
            i = end + 3
            continue

        # 3. Code span `...` — same rule, single backtick.
        if ch == "`":
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == "`":
                    break
                j += 1
            if j >= n:
                problems.append(Md2Problem(i, "`", "unclosed code span", _excerpt(text, i)))
                break
            i = j + 1
            continue

        # 4. Inline link [label](url). The URL half has its own escaping rules
        #    (only ) and \ are special), so skip it wholesale.
        if ch == "[":
            close = _match_link(text, i)
            if close is not None:
                label_end, url_end = close
                # Validate the label as ordinary text, skip the URL.
                problems.extend(
                    Md2Problem(p.index + i + 1, p.char, p.reason, p.excerpt)
                    for p in find_problems(text[i + 1 : label_end])
                )
                i = url_end + 1
                continue
            # A bare '[' that opens no link must be escaped.
            problems.append(Md2Problem(i, ch, "unescaped reserved character", _excerpt(text, i)))
            i += 1
            continue

        # 5. Entity delimiters: toggle and move on.
        for delim in ("||", "__", "*", "_", "~"):
            if text.startswith(delim, i):
                if open_delims and open_delims[-1][0] == delim:
                    open_delims.pop()
                else:
                    open_delims.append((delim, i))
                i += len(delim)
                break
        else:
            # 6. Anything else reserved and unescaped is a hard parse error.
            if ch in RESERVED:
                problems.append(Md2Problem(i, ch, "unescaped reserved character", _excerpt(text, i)))
            i += 1
            continue

    for delim, idx in open_delims:
        problems.append(Md2Problem(idx, delim, "unclosed entity delimiter", _excerpt(text, idx)))

    return problems


def _match_link(text: str, start: int) -> tuple[int, int] | None:
    """If ``text[start]`` opens a ``[label](url)``, return (label_end, url_end)."""
    i = start + 1
    n = len(text)
    while i < n:
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == "]":
            break
        if text[i] == "\n":
            return None
        i += 1
    if i >= n or text[i] != "]":
        return None
    label_end = i
    if i + 1 >= n or text[i + 1] != "(":
        return None
    j = i + 2
    while j < n:
        if text[j] == "\\":
            j += 2
            continue
        if text[j] == ")":
            return label_end, j
        if text[j] == "\n":
            return None
        j += 1
    return None


def is_valid(text: str) -> bool:
    return not find_problems(text)
