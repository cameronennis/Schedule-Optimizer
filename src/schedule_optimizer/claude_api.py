"""API-key mode: chat with Claude inside the app, billed to the user's Claude Console key.

The key lives in the system keychain (keyring), never in the database or a file.
A monthly spending cap is checked before every request; spending is estimated from
each response's token usage at list prices.

Claude gets the read tools, `solve` and `propose_changes` (see claude_tools). It
can't apply changes: proposals show up in the app with Apply and Dismiss buttons.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

import anthropic
import keyring
from keyring.errors import PasswordDeleteError
from sqlmodel import Session

from . import claude_tools, store
from .tables import ApiUsage

KEYRING_SERVICE = "Schedule Optimizer"
KEYRING_USER = "anthropic-api-key"
MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_TOOL_ROUNDS = 10

# USD per million tokens: (input, output, cache read). Cache writes cost 1.25× input.
# A refused request may be re-run on a fallback model, billed at that model's rates, so a
# few likely fallbacks are listed too. Unknown models are priced at a high worst case.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-opus-5": (5.00, 25.00, 0.50),
    "claude-opus-4-8": (5.00, 25.00, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00, 0.10),
    "claude-sonnet-5": (2.00, 10.00, 0.20),
}
WORST_CASE = (10.00, 50.00, 1.00)


class CapReached(Exception):
    pass


class NoKey(Exception):
    pass


# ---- the key ---------------------------------------------------------------------------------


def get_key() -> str | None:
    return keyring.get_password(KEYRING_SERVICE, KEYRING_USER)


def has_key() -> bool:
    return bool(get_key())


def reachable(timeout: float = 2.0) -> bool:
    """Is the Claude API reachable from here (a quick connection test, nothing sent)?"""
    import socket

    try:
        socket.create_connection(("api.anthropic.com", 443), timeout=timeout).close()
        return True
    except OSError:
        return False


def check_key(key: str) -> None:
    """A free test call (looks up the model). Raises anthropic errors if the key is bad."""
    anthropic.Anthropic(api_key=key, max_retries=1, timeout=20.0).models.retrieve(MODEL)


def save_key(key: str) -> None:
    key = key.strip()
    if not key:
        raise ValueError("Paste an API key from the Claude Console.")
    check_key(key)
    keyring.set_password(KEYRING_SERVICE, KEYRING_USER, key)


def forget_key() -> None:
    try:
        keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
    except PasswordDeleteError:
        pass


# ---- spending ----------------------------------------------------------------------------------


def _month(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m")


def month_usage(s: Session) -> ApiUsage:
    return s.get(ApiUsage, _month()) or ApiUsage(month=_month())


def _cost(model: str, usage) -> float:
    inp, out, read = PRICES.get(model, WORST_CASE)
    fresh = usage.input_tokens or 0
    written = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cached = getattr(usage, "cache_read_input_tokens", 0) or 0
    return (fresh * inp + written * inp * 1.25 + cached * read + (usage.output_tokens or 0) * out) / 1e6


def record_usage(s: Session, model: str, usage) -> float:
    row = month_usage(s)
    cost = _cost(model, usage)
    row.input_tokens += (usage.input_tokens or 0) + (getattr(usage, "cache_creation_input_tokens", 0) or 0) \
        + (getattr(usage, "cache_read_input_tokens", 0) or 0)
    row.output_tokens += usage.output_tokens or 0
    row.cost_usd += cost
    s.add(row)
    s.commit()
    return cost


def check_cap(s: Session) -> None:
    cap = store.get_prefs(s).claude_monthly_cap_usd
    spent = month_usage(s).cost_usd
    if spent >= cap:
        raise CapReached(f"This month's Claude spending (${spent:.2f}) reached your ${cap:.2f} cap. "
                         "Raise it in Preferences to keep chatting.")


# ---- chat ----------------------------------------------------------------------------------------


def tool_specs() -> list[dict]:
    return [{"name": n, "description": claude_tools.TOOLS[n]["description"],
             "input_schema": claude_tools.TOOLS[n]["input_schema"]} for n in claude_tools.API_TOOLS]


@dataclass
class Chat:
    """One conversation. `messages` is the API history; `log` is what the window shows."""

    messages: list = field(default_factory=list)
    log: list[tuple[str, str]] = field(default_factory=list)  # (role, text); role: user, claude, tool, error
    proposals: list[int] = field(default_factory=list)  # proposal ids created in this chat


def send(chat: Chat, text: str, session_factory, now_fn, client: anthropic.Anthropic | None = None) -> None:
    """Send one user message and run the tool loop until Claude answers. Blocking."""
    key = get_key() if client is None else "unused"
    if not key:
        raise NoKey("Add your Claude API key in Preferences first.")
    client = client or anthropic.Anthropic(api_key=key)
    ctx = claude_tools.Ctx(source="api", now=now_fn)
    chat.messages.append({"role": "user", "content": text})
    chat.log.append(("user", text))

    for _ in range(MAX_TOOL_ROUNDS):
        with session_factory() as s:
            check_cap(s)
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            system=claude_tools.INSTRUCTIONS + claude_tools.API_INSTRUCTIONS,
            tools=tool_specs(),
            messages=chat.messages,
            output_config={"effort": "medium"},
            cache_control={"type": "ephemeral"},
            betas=[FALLBACK_BETA],
            fallbacks="default",
        )
        with session_factory() as s:
            record_usage(s, response.model, response.usage)
        chat.messages.append({"role": "assistant", "content": response.content})
        for block in response.content:
            if block.type == "text" and block.text.strip():
                chat.log.append(("claude", block.text))

        if response.stop_reason == "refusal":
            chat.log.append(("error", "Claude declined to answer that."))
            return
        if response.stop_reason == "max_tokens":
            chat.log.append(("error", "The reply was cut off (too long)."))
            return
        calls = [b for b in response.content if b.type == "tool_use"]
        if response.stop_reason != "tool_use" or not calls:
            return

        results = []
        for call in calls:
            if call.name not in claude_tools.API_TOOLS:
                out, is_error = {"error": f"{call.name} isn't available in the app"}, True
            else:
                try:
                    with session_factory() as s:
                        out, is_error = claude_tools.run_tool(s, call.name, dict(call.input or {}), ctx), False
                    if call.name == "propose_changes":
                        chat.proposals.append(out["proposal_id"])
                except (ValueError, KeyError) as e:
                    out, is_error = {"error": e.args[0] if isinstance(e, KeyError) and e.args else str(e)}, True
            chat.log.append(("tool", _tool_note(call.name, out, is_error)))
            results.append({"type": "tool_result", "tool_use_id": call.id, "is_error": is_error,
                            "content": json.dumps(out, ensure_ascii=False, default=str)})
        chat.messages.append({"role": "user", "content": results})
    chat.log.append(("error", "Stopped after too many tool calls in a row."))


def _tool_note(name: str, out: dict, is_error: bool) -> str:
    if is_error:
        return f"{name} failed: {out.get('error')}"
    return {"get_plan": "Read the plan", "get_tasks": "Read the tasks", "explain_block": "Looked up a block",
            "solve": "Re-planned", "propose_changes": f"Proposed changes (#{out.get('proposal_id')})"}.get(name, name)
