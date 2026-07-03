"""HTML preprocessing for LLM consumption.

Strips noise while preserving structural markers and key attributes
(href, class, id, src, alt, title) that the LLM needs for extraction.
"""

from __future__ import annotations

import re

# Elements to remove entirely
_REMOVE_TAGS = re.compile(
    r"<(script|style|svg|noscript|head|meta|link|iframe)[^>]*>.*?</\1>",
    re.DOTALL | re.IGNORECASE,
)
# Self-closing removes
_REMOVE_SELF_CLOSING = re.compile(
    r"<(meta|link|input|br|hr|img)[^>]*/?>",
    re.IGNORECASE,
)

_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

# Attributes to keep
_KEEP_ATTRS = {"href", "class", "id", "src", "alt", "title", "name", "type", "value", "target"}
_ATTR_RE = re.compile(
    r'\s(\w[\w-]*)(?:\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|(\S+)))?',
)

_LONG_WHITESPACE_RE = re.compile(r"\s{3,}")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")


def clean_html(html: str) -> str:
    """Strip noise, compress attributes, but keep DOM structure and key attrs."""
    # Remove comments
    html = _COMMENT_RE.sub("", html)
    # Remove entire noise elements
    html = _REMOVE_TAGS.sub("", html)
    html = _REMOVE_SELF_CLOSING.sub("", html)

    # Compress attributes: keep only href, class, id, src, alt, title, etc.
    html = _compress_attrs(html)

    # Collapse whitespace
    html = _LONG_WHITESPACE_RE.sub(" ", html)
    html = _MULTI_NEWLINE_RE.sub("\n\n", html)
    # Remove empty lines
    html = "\n".join(line for line in html.splitlines() if line.strip())

    return html


def _compress_attrs(html: str) -> str:
    """Keep only important attributes, strip the rest."""

    def _replace(m: re.Match[str]) -> str:
        tag = m.group(0)
        # Find opening tag
        tag_match = re.match(r"<\w+", tag)
        if not tag_match:
            return tag
        tag_start = tag_match.group()
        # Parse attributes
        attrs = _ATTR_RE.findall(tag)
        kept = []
        for match in attrs:
            attr_name = match[0].lower()
            if attr_name in _KEEP_ATTRS:
                kept.append(match)
        # Rebuild tag
        result = tag_start
        for a in kept:
            name = a[0]
            val = a[1] or a[2] or a[3] or ""
            if val:
                result += f' {name}="{val}"'
            else:
                result += f" {name}"
        result += ">"
        return result

    # Find all HTML tags and compress attributes
    return re.sub(r"<\w+[^>]*>", _replace, html)
