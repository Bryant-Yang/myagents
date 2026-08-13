"""Safe, lightweight Markdown rendering for chat messages in a RichLog."""

from __future__ import annotations

import re

from rich.text import Text


_INLINE_RE = re.compile(
    r"(?P<code>`(?P<code_text>[^`\n]+)`)|"
    r"(?P<strong>(?<![A-Za-z0-9_*])\*\*(?=[^\s/*])"
    r"(?P<strong_text>\S(?:.*?\S)?)\*\*"
    r"(?![A-Za-z0-9_*]))|"
    r"(?P<em>(?<![A-Za-z0-9_*])\*(?=[^\s/*])"
    r"(?P<em_text>\S(?:[^*\n]*?\S)?)\*(?![A-Za-z0-9_*]))"
)
_FENCE_RE = re.compile(r"^\s*```[^`]*$", re.IGNORECASE)
_HEADING_RE = re.compile(r"^\s{0,3}(?P<marks>#{1,6})\s+(?P<body>.*?)\s*#*\s*$")
_BULLET_RE = re.compile(r"^(?P<indent>\s*)[-+*]\s+(?P<body>.*)$")
_ORDERED_RE = re.compile(r"^(?P<indent>\s*)(?P<number>\d+[.)])\s+(?P<body>.*)$")
_QUOTE_RE = re.compile(r"^(?P<indent>\s*)>\s?(?P<body>.*)$")


def _mask_escaped_delimiters(value: str) -> tuple[str, dict[str, str]]:
    """Replace escaped delimiters with same-width private placeholders."""
    placeholders: dict[str, str] = {}
    candidates = (chr(codepoint) for codepoint in range(0xE000, 0xF900))

    def placeholder(character: str) -> str:
        for token, original in placeholders.items():
            if original == character:
                return token
        token = next(candidate for candidate in candidates
                     if candidate not in value and candidate not in placeholders)
        placeholders[token] = character
        return token

    masked: list[str] = []
    index = 0
    while index < len(value):
        if (value[index] == "\\" and index + 1 < len(value)
                and value[index + 1] in {"\\", "*", "`"}):
            escaped = value[index + 1]
            run_length = 1
            if escaped in {"*", "`"}:
                while (index + 1 + run_length < len(value)
                       and value[index + 1 + run_length] == escaped):
                    run_length += 1
            masked.extend(placeholder(escaped) for _ in range(run_length))
            index += 1 + run_length
            continue
        masked.append(value[index])
        index += 1
    return "".join(masked), placeholders


def _restore_escaped_delimiters(
        value: str, placeholders: dict[str, str]) -> str:
    if not placeholders:
        return value
    return "".join(placeholders.get(character, character) for character in value)


def _append_inline(
        rendered: Text,
        value: str,
        placeholders: dict[str, str],
) -> None:
    cursor = 0
    for match in _INLINE_RE.finditer(value):
        rendered.append(_restore_escaped_delimiters(
            value[cursor:match.start()], placeholders))
        if match.group("code") is not None:
            rendered.append(
                _restore_escaped_delimiters(
                    match.group("code_text"), placeholders),
                "bold cyan on grey15",
            )
        elif match.group("strong") is not None:
            rendered.append(_restore_escaped_delimiters(
                match.group("strong_text"), placeholders), "bold")
        else:
            rendered.append(_restore_escaped_delimiters(
                match.group("em_text"), placeholders), "italic")
        cursor = match.end()
    rendered.append(_restore_escaped_delimiters(value[cursor:], placeholders))


def render_chat_markdown(value: str, base_style: str = "") -> Text:
    """Render common chat Markdown without accepting Rich markup.

    The renderer intentionally supports only presentation syntax commonly
    emitted by agents. Unknown or incomplete Markdown stays literal, so a
    streaming reply can be redrawn safely while it is still growing.
    """
    value, placeholders = _mask_escaped_delimiters(value)
    rendered = Text(style=base_style)
    in_fence = False
    lines = value.splitlines(keepends=True)
    if not lines and value == "":
        return rendered
    fence_lines = [
        index
        for index, raw_line in enumerate(lines)
        if _FENCE_RE.fullmatch(raw_line.rstrip("\r\n"))
    ]
    unmatched_fence = (
        fence_lines[-1] if len(fence_lines) % 2 else None)

    for index, raw_line in enumerate(lines):
        has_newline = raw_line.endswith(("\n", "\r"))
        line = raw_line.rstrip("\r\n")
        if unmatched_fence is not None and index >= unmatched_fence:
            rendered.append(_restore_escaped_delimiters(line, placeholders))
            if has_newline:
                rendered.append("\n")
            continue
        if _FENCE_RE.fullmatch(line):
            in_fence = not in_fence
            continue
        if in_fence:
            rendered.append(
                _restore_escaped_delimiters(line, placeholders),
                "cyan on grey15",
            )
        else:
            heading = _HEADING_RE.fullmatch(line)
            bullet = _BULLET_RE.fullmatch(line)
            ordered = _ORDERED_RE.fullmatch(line)
            quote = _QUOTE_RE.fullmatch(line)
            if heading is not None:
                heading_style = (
                    "bold underline bright_white"
                    if len(heading.group("marks")) == 1
                    else "bold bright_white"
                )
                start = len(rendered)
                _append_inline(rendered, heading.group("body"), placeholders)
                rendered.stylize(heading_style, start, len(rendered))
            elif bullet is not None:
                rendered.append(bullet.group("indent"))
                rendered.append("• ", "bold cyan")
                _append_inline(rendered, bullet.group("body"), placeholders)
            elif ordered is not None:
                rendered.append(ordered.group("indent"))
                rendered.append(ordered.group("number") + " ", "bold cyan")
                _append_inline(rendered, ordered.group("body"), placeholders)
            elif quote is not None:
                rendered.append(quote.group("indent"))
                rendered.append("│ ", "bright_black")
                start = len(rendered)
                _append_inline(rendered, quote.group("body"), placeholders)
                rendered.stylize("italic", start, len(rendered))
            else:
                _append_inline(rendered, line, placeholders)
        if has_newline:
            rendered.append("\n")
    return rendered
