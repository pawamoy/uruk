"""Convert the agent's standard markdown to Telegram's HTML subset.

Telegram only formats messages that carry MessageEntity spans. Rather than build
entities by hand, we send HTML with parse_mode=HTML and let Telegram's server
parse it into entities. Telegram supports a small tag set (b, i, s, u, code,
pre, a, blockquote), so block constructs it lacks are approximated: headings
become bold, list markers become bullets, tables are wrapped in <pre>.
"""

from __future__ import annotations

import html
import re

_FENCE = re.compile(r"^\s*```(\w[\w+#.-]*)?\s*$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_QUOTE = re.compile(r"^>\s?(.*)$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+")
_HRULE = re.compile(r"^\s*([-*_])\s*(\1\s*){2,}$")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")

_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_BOLD = re.compile(r"(?<!\*)\*\*(?!\s)(.+?)(?<!\s)\*\*(?!\*)")
_ITALIC_STAR = re.compile(r"(?<![*\w])\*(?![\s*])([^*\n]+)(?<![\s*])\*(?![*\w])")
_ITALIC_UNDER = re.compile(r"(?<![\w\\])_(?![\s_])([^_\n]+)(?<![\s_])_(?!\w)")
_STRIKE = re.compile(r"~~(?!\s)(.+?)(?<!\s)~~")


def md_to_html(text: str) -> str:
    """Render markdown as Telegram HTML, one block construct at a time."""
    out: list[str] = []
    lines = text.replace("\x00", "").split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if fence := _FENCE.match(line):
            block: list[str] = []
            i += 1
            while i < len(lines) and not _FENCE.match(lines[i]):
                block.append(lines[i])
                i += 1
            i += 1  # Skip the closing fence (harmless at end of input).
            code = html.escape("\n".join(block))
            if lang := fence.group(1):
                out.append(f'<pre><code class="language-{html.escape(lang)}">{code}</code></pre>')
            else:
                out.append(f"<pre>{code}</pre>")
        elif _QUOTE.match(line):
            quoted: list[str] = []
            while i < len(lines) and (quote := _QUOTE.match(lines[i])):
                quoted.append(_inline(quote.group(1)))
                i += 1
            out.append("<blockquote>{}</blockquote>".format("\n".join(quoted)))
        elif _TABLE_ROW.match(line):
            rows: list[str] = []
            while i < len(lines) and _TABLE_ROW.match(lines[i]):
                rows.append(html.escape(lines[i]))
                i += 1
            out.append("<pre>{}</pre>".format("\n".join(rows)))
        elif heading := _HEADING.match(line):
            out.append(f"<b>{_inline(heading.group(2))}</b>")
            i += 1
        elif _HRULE.match(line):
            out.append("———")
            i += 1
        else:
            out.append(_inline(_BULLET.sub(r"\1• ", line)))
            i += 1
    return "\n".join(out)


def _inline(line: str) -> str:
    """Handle span-level markdown, shielding code spans from the other rules."""
    spans: list[str] = []

    def stash(match: re.Match) -> str:
        spans.append(f"<code>{html.escape(match.group(1))}</code>")
        return f"\x00{len(spans) - 1}\x00"

    line = _CODE_SPAN.sub(stash, line)
    line = html.escape(line)
    line = _LINK.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', line)
    line = _BOLD.sub(r"<b>\1</b>", line)
    line = _ITALIC_STAR.sub(r"<i>\1</i>", line)
    line = _ITALIC_UNDER.sub(r"<i>\1</i>", line)
    line = _STRIKE.sub(r"<s>\1</s>", line)
    return re.sub(r"\x00(\d+)\x00", lambda m: spans[int(m.group(1))], line)
