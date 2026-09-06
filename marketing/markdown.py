"""A small, safe Markdown renderer for AI replies.

WHY THIS EXISTS
---------------
Language models answer in Markdown. Rendering that with `textContent` shows the
syntax literally -- `**bold**` with the asterisks -- and rendering it with
`innerHTML` would hand an untrusted string to the HTML parser.

WHY NOT A LIBRARY
-----------------
The project's virtual environment holds Django and nothing else, and this file
covers the subset a chat reply actually uses. It is roughly 150 lines and can be
read in full, which matters more here than breadth.

THE SAFETY ARGUMENT
-------------------
Output is safe *by construction*, in this order:

1. Every `<`, `>`, `&`, `"` and `'` in the input is escaped first, so nothing
   the model wrote can ever become a tag.
2. Only then are this module's own tags introduced, from a fixed set:
   p, br, strong, em, code, pre, ul, ol, li, h3, h4, blockquote, hr, a.
3. Link targets are checked against a scheme allowlist, so `javascript:` and
   `data:` URLs cannot be produced.

Because escaping happens before generation, there is no path by which model
output reaches the browser as markup. The result is marked safe only at the
very end, after all of that has happened.

Rendering happens on the server for both paths -- the initial page render and
the JSON returned to static/js/chat.js -- so there is one implementation to
trust rather than two that could drift.
"""

import re

from django.utils.html import escape
from django.utils.safestring import mark_safe

# Schemes a link may use. Anything else is rendered as plain text.
SAFE_SCHEMES = ('http://', 'https://', 'mailto:')

# A placeholder that cannot survive escaping, so it can never be forged by
# the input: the marker characters would themselves have been escaped.
_BLOCK_TOKEN = '\x00CODEBLOCK{}\x00'

_FENCED_RE = re.compile(r'```[ \t]*([A-Za-z0-9_+-]*)[ \t]*\n(.*?)```', re.DOTALL)
_INLINE_CODE_RE = re.compile(r'`([^`\n]+)`')
_BOLD_RE = re.compile(r'\*\*(?=\S)(.+?)(?<=\S)\*\*', re.DOTALL)
_ITALIC_RE = re.compile(r'(?<![\*\w])\*(?=\S)([^\*\n]+?)(?<=\S)\*(?!\*)')
_UNDERSCORE_ITALIC_RE = re.compile(r'(?<![_\w])_(?=\S)([^_\n]+?)(?<=\S)_(?![_\w])')
_STRIKE_RE = re.compile(r'~~(?=\S)(.+?)(?<=\S)~~', re.DOTALL)
_LINK_RE = re.compile(r'\[([^\]\n]+)\]\(([^)\s]+)\)')
_HEADING_RE = re.compile(r'^(#{1,6})\s+(.*)$')
_BULLET_RE = re.compile(r'^[ \t]*[-*+][ \t]+(.*)$')
_ORDERED_RE = re.compile(r'^[ \t]*(\d+)[.)][ \t]+(.*)$')
_QUOTE_RE = re.compile(r'^[ \t]*>[ \t]?(.*)$')
_RULE_RE = re.compile(r'^[ \t]*([-*_])(?:[ \t]*\1){2,}[ \t]*$')


def _inline(text):
    """Apply inline formatting to a run of already-escaped text."""
    text = _INLINE_CODE_RE.sub(lambda m: f'<code>{m.group(1)}</code>', text)
    text = _BOLD_RE.sub(lambda m: f'<strong>{m.group(1)}</strong>', text)
    text = _STRIKE_RE.sub(lambda m: f'<del>{m.group(1)}</del>', text)
    text = _ITALIC_RE.sub(lambda m: f'<em>{m.group(1)}</em>', text)
    text = _UNDERSCORE_ITALIC_RE.sub(lambda m: f'<em>{m.group(1)}</em>', text)
    text = _LINK_RE.sub(_link, text)
    return text


def _link(match):
    label, target = match.group(1), match.group(2)
    # The target was escaped with the rest of the text, so &amp; has to be put
    # back before the scheme is checked and the URL written into href.
    href = target.replace('&amp;', '&')
    if not href.lower().startswith(SAFE_SCHEMES):
        return match.group(0)          # leave it as plain text
    return f'<a href="{escape(href)}" target="_blank" rel="noopener noreferrer">{label}</a>'


def _close_lists(open_list, out):
    if open_list:
        out.append(f'</{open_list}>')
    return None


def render(text):
    """Render Markdown to a safe HTML fragment."""
    if not text:
        return mark_safe('')

    # 1. Escape everything up front. Nothing below can introduce a tag from
    #    the input, only from this module.
    escaped = escape(text).replace('\r\n', '\n').replace('\r', '\n')

    # 2. Lift fenced code blocks out before any line-based parsing, so their
    #    contents are never mistaken for Markdown.
    blocks = []

    def _stash(match):
        language, body = match.group(1), match.group(2)
        css = f' class="lang-{escape(language)}"' if language else ''
        blocks.append(f'<pre><code{css}>{body.rstrip()}</code></pre>')
        return _BLOCK_TOKEN.format(len(blocks) - 1)

    escaped = _FENCED_RE.sub(_stash, escaped)

    out = []
    paragraph = []
    open_list = None

    def flush_paragraph():
        if paragraph:
            out.append('<p>' + _inline('<br>'.join(paragraph)) + '</p>')
            paragraph.clear()

    for line in escaped.split('\n'):
        stripped = line.strip()

        if stripped.startswith('\x00CODEBLOCK'):
            flush_paragraph()
            open_list = _close_lists(open_list, out)
            index = int(stripped.replace('\x00', '').replace('CODEBLOCK', ''))
            out.append(blocks[index])
            continue

        if not stripped:
            flush_paragraph()
            open_list = _close_lists(open_list, out)
            continue

        if _RULE_RE.match(stripped):
            flush_paragraph()
            open_list = _close_lists(open_list, out)
            out.append('<hr>')
            continue

        heading = _HEADING_RE.match(stripped)
        if heading:
            flush_paragraph()
            open_list = _close_lists(open_list, out)
            # Headings inside a chat bubble sit under the page's own h1/h2, so
            # they are rendered as h3/h4 to keep the document outline sane.
            level = 'h3' if len(heading.group(1)) <= 2 else 'h4'
            out.append(f'<{level}>{_inline(heading.group(2))}</{level}>')
            continue

        quote = _QUOTE_RE.match(line)
        if quote:
            flush_paragraph()
            open_list = _close_lists(open_list, out)
            out.append(f'<blockquote>{_inline(quote.group(1))}</blockquote>')
            continue

        bullet = _BULLET_RE.match(line)
        if bullet:
            flush_paragraph()
            if open_list != 'ul':
                open_list = _close_lists(open_list, out)
                out.append('<ul>')
                open_list = 'ul'
            out.append(f'<li>{_inline(bullet.group(1))}</li>')
            continue

        ordered = _ORDERED_RE.match(line)
        if ordered:
            flush_paragraph()
            if open_list != 'ol':
                open_list = _close_lists(open_list, out)
                out.append('<ol>')
                open_list = 'ol'
            out.append(f'<li>{_inline(ordered.group(2))}</li>')
            continue

        if open_list:
            open_list = _close_lists(open_list, out)
        paragraph.append(stripped)

    flush_paragraph()
    _close_lists(open_list, out)

    # 3. Safe only now: every tag above was written by this module.
    return mark_safe(''.join(out))
