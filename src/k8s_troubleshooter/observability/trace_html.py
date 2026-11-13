"""One run as a single HTML page: a timeline of the steps, each one opening to its full input and output.

The page has no script and loads nothing from the network. Every value from the trace is escaped,
because it holds cluster text (logs, events) that anyone with access to the cluster can write.
"""

from __future__ import annotations

import json
from html import escape

from k8s_troubleshooter.observability.trace_view import step_summary

HEADER_KEYS = ("step", "type", "span", "started_at_ms", "duration_ms", "status")
LABELS = {"model": "model", "tool": "tool", "check": "check"}

STYLE = """
:root { --model: #4f6bed; --tool: #1f9d6b; --check: #8a5cd1; --error: #d64545; --line: #e3e6ec; --muted: #687083; }
* { box-sizing: border-box; }
body { margin: 0; font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: #1d2130; background: #f6f7f9; }
main { max-width: 1100px; margin: 0 auto; padding: 28px 24px 60px; }
h1 { font-size: 20px; margin: 0 0 4px; }
h2 { font-size: 15px; margin: 28px 0 10px; }
h4 { margin: 12px 0 4px; font-size: 12px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); }
.ids { color: var(--muted); font-size: 12px; font-family: ui-monospace, Menlo, monospace; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin-top: 16px; }
.card { background: #fff; border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; }
.card b { display: block; font-size: 18px; }
.card span { color: var(--muted); font-size: 12px; }
.badge { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 12px; font-weight: 600; color: #fff; }
.ok { background: var(--tool); } .error { background: var(--error); }
.steps { background: #fff; border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
details { border-top: 1px solid var(--line); } details:first-child { border-top: 0; }
summary { display: grid; grid-template-columns: 28px 210px 1fr 70px; gap: 10px; align-items: center; padding: 8px 12px; cursor: pointer; list-style: none; }
summary::-webkit-details-marker { display: none; }
summary:hover { background: #f9fafc; }
.num { color: var(--muted); font-size: 12px; text-align: right; }
.name { font-family: ui-monospace, Menlo, monospace; font-size: 12.5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.name i { font-style: normal; font-size: 10px; font-weight: 700; text-transform: uppercase; margin-right: 6px; padding: 1px 5px; border-radius: 4px; color: #fff; }
.what { display: grid; gap: 4px; min-width: 0; }
.what div { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.track { position: relative; height: 6px; background: #eef0f4; border-radius: 3px; }
.bar { position: absolute; top: 0; height: 6px; border-radius: 3px; min-width: 3px; }
.ms { text-align: right; color: var(--muted); font-size: 12px; font-variant-numeric: tabular-nums; }
.model .bar, .model i { background: var(--model); }
.tool .bar, .tool i { background: var(--tool); }
.check .bar, .check i { background: var(--check); }
.failed .bar, .failed i { background: var(--error); }
.failed .what div { color: var(--error); }
.body { padding: 4px 16px 14px 50px; background: #fbfbfd; border-top: 1px dashed var(--line); }
pre { margin: 0; padding: 10px; background: #fff; border: 1px solid var(--line); border-radius: 6px; overflow: auto; max-height: 420px;
      font: 12px/1.45 ui-monospace, Menlo, monospace; white-space: pre-wrap; word-break: break-word; }
.answer { background: #fff; border: 1px solid var(--line); border-radius: 8px; padding: 6px 16px 14px; }
.answer dt { margin-top: 10px; font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }
.answer dd { margin: 2px 0 0; }
.answer li { margin: 4px 0; }
code { font-family: ui-monospace, Menlo, monospace; font-size: 12.5px; background: #eef0f4; padding: 0 4px; border-radius: 4px; }
.hint { color: var(--muted); font-size: 12px; margin-top: 6px; }
.logs { background: #fff; border: 1px solid var(--line); border-radius: 8px; overflow: auto; }
.logs table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
.logs th { text-align: left; font-weight: 600; color: var(--muted); padding: 7px 10px; border-bottom: 1px solid var(--line); }
.logs td { padding: 5px 10px; border-top: 1px solid var(--line); vertical-align: top; white-space: nowrap; }
.logs td.fields { white-space: normal; word-break: break-word; font-family: ui-monospace, Menlo, monospace; font-size: 12px; }
.logs a { color: var(--model); }
.lvl-warning td:nth-child(2) { color: #b7791f; font-weight: 600; }
.lvl-error td:nth-child(2), .lvl-critical td:nth-child(2) { color: var(--error); font-weight: 600; }
.lvl-debug { color: var(--muted); }
:target summary { background: #eef2ff; }
"""


def render_html(body: dict) -> str:
    request, summary = body["request"], body["summary"]
    title = request.get("question") or request.get("change") or body["run_id"]
    total = max(float(summary["duration_ms"]), 1.0)
    return "\n".join([
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'\">",
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{_e(title)} · k8s-troubleshooter trace</title>",
        f"<style>{STYLE}</style></head><body><main>",
        f"<h1>{_e(title)}</h1>",
        f'<div class="ids">run {_e(body["run_id"])} · trace {_e(body["trace_id"])}</div>',
        _cards(request, summary),
        _linked(body),
        "<h2>Steps</h2>",
        '<div class="steps">' + "".join(_step(step, total) for step in body["steps"]) + "</div>",
        '<p class="hint">Click a step to see its full input and output.</p>',
        "<h2>Answer</h2>",
        _answer(body.get("response")),
        "<h2>Logs</h2>",
        _logs(body.get("logs") or []),
        "</main></body></html>",
    ])


def _cards(request: dict, summary: dict) -> str:
    tokens = summary["tokens"]
    status = "error" if summary["status"] == "error" else "ok"
    cards = [
        (f'<span class="badge {status}">{_e(summary["result"])}</span>', "result"),
        (_e(f'{summary["duration_ms"] / 1000:.1f} s'), "took"),
        (_e(str(summary["model_calls"])), "model calls"),
        (_e(str(summary["tool_calls"])), "tool calls"),
        (_e(str(summary["errors"])), "errors"),
    ]
    if tokens["total"]:
        cards.append((_e(str(tokens["total"])), f'tokens ({tokens["input"]} in, {tokens["output"]} out)'))
    if request.get("model"):
        cards.append((_e(request["model"]), f'model ({_e(request.get("provider", ""))})'))
    html = "".join(f'<div class="card"><b>{value}</b><span>{_e(label)}</span></div>' for value, label in cards)
    if summary.get("error"):
        html += f'<div class="card"><b>failed</b><span>{_e(summary["error"])}</span></div>'
    return f'<div class="cards">{html}</div>'


def _linked(body: dict) -> str:
    linked = body.get("linked_trace")
    if not linked:
        return ""
    return f'<p class="hint">Proposed by the investigation in trace <code>{_e(linked["trace_id"])}</code>.</p>'


def _step(step: dict, total: float) -> str:
    kind = step["type"] + (" failed" if step["status"] == "error" else "")
    left = min(step["started_at_ms"] / total * 100, 99.5)
    width = min(max(step["duration_ms"] / total * 100, 0.3), 100 - left)
    details = "".join(
        f"<h4>{_e(key.replace('_', ' '))}</h4><pre>{_e(_pretty(value))}</pre>"
        for key, value in step.items() if key not in HEADER_KEYS
    )
    return (
        f'<details class="{kind}" id="step-{step["step"]}"><summary>'
        f'<span class="num">{step["step"]}</span>'
        f'<span class="name"><i>{LABELS.get(step["type"], step["type"])}</i>{_e(step["span"])}</span>'
        f'<span class="what"><div>{_e(step_summary(step))}</div>'
        f'<span class="track"><span class="bar" style="left:{left:.2f}%;width:{width:.2f}%"></span></span></span>'
        f'<span class="ms">{step["duration_ms"]:.0f} ms</span>'
        f'</summary><div class="body">{details or "<h4>no details</h4>"}</div></details>'
    )


def _answer(response) -> str:
    if not isinstance(response, dict) or not response:
        return '<div class="answer"><p>No answer.</p></div>'
    if "cause" not in response:
        rows = "".join(f"<dt>{_e(key.replace('_', ' '))}</dt><dd>{_e(_pretty(value))}</dd>" for key, value in response.items())
        return f'<div class="answer"><dl>{rows}</dl></div>'
    evidence = "".join(
        f"<li><code>{_e(item.get('action', ''))}</code> {_e(item.get('fact', ''))}</li>" for item in response.get("evidence") or []
    )
    fix = response.get("proposed_fix")
    fix_text = (
        f"Health-check port {fix.get('from_port')} to {fix.get('to_port')} on {fix.get('namespace')}/{fix.get('deployment')}. "
        "Run approve with the proposal id to apply it."
        if fix else "None"
    )
    return (
        '<div class="answer"><dl>'
        f"<dt>Fault found</dt><dd>{'yes' if response.get('fault_found') else 'no'}</dd>"
        f"<dt>Cause</dt><dd>{_e(response.get('cause', ''))}</dd>"
        f"<dt>Evidence</dt><dd><ul>{evidence or '<li>none</li>'}</ul></dd>"
        f"<dt>Next step</dt><dd>{_e(response.get('next_action', ''))}</dd>"
        f"<dt>Proposed fix</dt><dd>{_e(fix_text)}</dd>"
        "</dl></div>"
    )


def _logs(lines: list[dict]) -> str:
    if not lines:
        return '<p class="hint">No log lines for this run. Logs are saved from the CLI commands, not from direct calls.</p>'
    rows = []
    for line in lines:
        fields = " ".join(
            f"{key}={_compact(value)}" for key, value in line.items() if key not in ("time", "level", "event", "step", "service")
        )
        step = f'<a href="#step-{line["step"]}">step {line["step"]}</a>' if line.get("step") else "run"
        level = str(line.get("level") or "")
        rows.append(
            f'<tr class="lvl-{_e(level.lower())}"><td>{_e(str(line.get("time") or "")[11:23])}</td><td>{_e(level)}</td>'
            f'<td><code>{_e(line.get("event", ""))}</code></td><td>{step}</td><td class="fields">{_e(fields)}</td></tr>'
        )
    return (
        '<div class="logs"><table><thead><tr><th>Time (UTC)</th><th>Level</th><th>Event</th><th>Step</th><th>Fields</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table></div>'
        '<p class="hint">The same lines as <code>logs --run</code> for this run. Click a step to jump to it.</p>'
    )


def _compact(value) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= 300 else text[:299] + "…"


def _pretty(value) -> str:
    return value if isinstance(value, str) else json.dumps(value, indent=2, ensure_ascii=False)


def _e(value) -> str:
    return escape(str(value), quote=True)
