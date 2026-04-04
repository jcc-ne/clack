"""Export session dialog to self-contained HTML."""

from __future__ import annotations

import html
import tempfile
import webbrowser
from pathlib import Path

from clack.models import DialogTurn, ToolCall

TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Claude Session: {title}</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #1a1a2e; color: #e0e0e0; padding: 2rem;
    max-width: 900px; margin: 0 auto; line-height: 1.6;
}}
.header {{
    background: #16213e; padding: 1.5rem; border-radius: 12px;
    margin-bottom: 2rem; border: 1px solid #0f3460;
}}
.header h1 {{ font-size: 1.4rem; color: #e94560; margin-bottom: 0.5rem; }}
.header .meta {{ color: #888; font-size: 0.85rem; }}
.turn {{ margin-bottom: 1.5rem; }}
.turn-user {{
    background: #1a3a5c; border-left: 4px solid #4fc3f7;
    padding: 1rem 1.5rem; border-radius: 0 12px 12px 0;
}}
.turn-assistant {{
    background: #1e1e30; border-left: 4px solid #66bb6a;
    padding: 1rem 1.5rem; border-radius: 0 12px 12px 0;
}}
.role {{
    font-weight: 700; font-size: 0.8rem; text-transform: uppercase;
    letter-spacing: 0.05em; margin-bottom: 0.5rem;
}}
.role-user {{ color: #4fc3f7; }}
.role-assistant {{ color: #66bb6a; }}
.meta-tags {{
    float: right; font-size: 0.75rem; color: #888;
    font-weight: 400; text-transform: none; letter-spacing: 0;
}}
.content {{ white-space: pre-wrap; word-break: break-word; }}
.tool-call {{
    margin: 0.75rem 0; background: #2a2a40; border-radius: 8px;
    border: 1px solid #3a3a50; overflow: hidden;
}}
.tool-call summary {{
    padding: 0.6rem 1rem; cursor: pointer; font-family: monospace;
    font-size: 0.85rem; color: #ffa726; background: #252535;
}}
.tool-call summary:hover {{ background: #303045; }}
.tool-call .tool-detail {{
    padding: 0.8rem 1rem; font-family: monospace; font-size: 0.8rem;
    max-height: 400px; overflow-y: auto; background: #1a1a28;
}}
.tool-call .tool-detail pre {{
    white-space: pre-wrap; word-break: break-word;
}}
.tool-input {{ color: #aaa; }}
.tool-result {{ color: #ccc; margin-top: 0.5rem; border-top: 1px solid #333; padding-top: 0.5rem; }}
.tool-error {{ color: #ef5350; }}
code {{
    background: #2a2a3e; padding: 0.15em 0.4em; border-radius: 4px;
    font-size: 0.9em;
}}
</style>
</head>
<body>
<div class="header">
    <h1>{title}</h1>
    <div class="meta">{meta}</div>
</div>
{turns_html}
</body>
</html>
"""


def export_dialog_html(
    session_id: str, title: str, dialog: list[DialogTurn]
) -> str:
    """Export dialog to HTML and open in browser. Returns file path."""
    turns_html = []
    for turn in dialog:
        if turn.role == "user":
            turns_html.append(_render_user_turn(turn))
        elif turn.role == "assistant":
            turns_html.append(_render_assistant_turn(turn))

    meta_parts = [f"Session: {session_id[:12]}..."]
    if dialog:
        meta_parts.append(f"Started: {dialog[0].timestamp[:19]}")
        models = {t.model for t in dialog if t.model}
        if models:
            meta_parts.append(f"Models: {', '.join(sorted(models))}")
    meta_parts.append(f"{len(dialog)} turns")

    html_content = TEMPLATE.format(
        title=html.escape(title),
        meta=html.escape(" | ".join(meta_parts)),
        turns_html="\n".join(turns_html),
    )

    path = Path(tempfile.gettempdir()) / f"clack-{session_id[:12]}.html"
    path.write_text(html_content)
    webbrowser.open(f"file://{path}")
    return str(path)


def _render_user_turn(turn: DialogTurn) -> str:
    return (
        f'<div class="turn turn-user">'
        f'<div class="role role-user">User</div>'
        f'<div class="content">{html.escape(turn.content)}</div>'
        f'</div>'
    )


def _render_assistant_turn(turn: DialogTurn) -> str:
    meta_parts = []
    if turn.model:
        meta_parts.append(turn.model.replace("claude-", ""))
    if turn.output_tokens:
        meta_parts.append(f"{turn.output_tokens} tokens")
    if turn.duration_ms:
        meta_parts.append(f"{turn.duration_ms / 1000:.1f}s")
    meta_tag = (
        f'<span class="meta-tags">{html.escape(" | ".join(meta_parts))}</span>'
        if meta_parts else ""
    )

    parts = [
        '<div class="turn turn-assistant">',
        f'<div class="role role-assistant">{meta_tag}Assistant</div>',
    ]

    if turn.content:
        parts.append(f'<div class="content">{html.escape(turn.content)}</div>')

    for tc in turn.tool_calls:
        parts.append(_render_tool_call(tc))

    parts.append("</div>")
    return "\n".join(parts)


def _render_tool_call(tc: ToolCall) -> str:
    error_cls = " tool-error" if tc.is_error else ""
    summary_text = _tool_summary(tc)

    detail_parts = []
    if tc.tool_input:
        inp_str = "\n".join(f"  {k}: {v}" for k, v in tc.tool_input.items())
        escaped_inp = html.escape(inp_str)
        detail_parts.append(
            f'<div class="tool-input"><strong>Input:</strong>'
            f"<pre>{escaped_inp}</pre></div>"
        )
    if tc.tool_result:
        escaped_result = html.escape(tc.tool_result[:3000])
        detail_parts.append(
            f'<div class="tool-result{error_cls}"><strong>Result:</strong>'
            f"<pre>{escaped_result}</pre></div>"
        )

    return (
        f'<details class="tool-call">'
        f'<summary>[{html.escape(tc.tool_name)}] {html.escape(summary_text)}</summary>'
        f'<div class="tool-detail">{"".join(detail_parts)}</div>'
        f'</details>'
    )


def _tool_summary(tc: ToolCall) -> str:
    inp = tc.tool_input
    if tc.tool_name == "Bash":
        return inp.get("command", "")[:80]
    elif tc.tool_name in ("Read", "Edit", "Write"):
        return inp.get("file_path", "")
    elif tc.tool_name in ("Grep", "Glob"):
        return inp.get("pattern", "")
    elif tc.tool_name == "Agent":
        return inp.get("description", "")[:60]
    return str(inp)[:60]
