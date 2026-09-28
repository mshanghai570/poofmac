# SPDX-License-Identifier: MIT
# Copyright (c) 2026 lesteroliver — https://poofmac.app
"""
LLM agent loop — the reasoning brain of PoofMac.

Architecture
────────────
• Uses LiteLLM for model abstraction (Ollama, Anthropic, OpenRouter, OpenAI).
• temperature=0  → deterministic, no creative hallucination on file paths.
• Tool calls only — the model cannot run arbitrary shell commands.
• Yields structured events so the TUI can update in real time.
• System prompt + code-level safety.py = belt-and-suspenders protection.

Event types yielded
───────────────────
  {"type": "status",     "text": str}
  {"type": "tool_call",  "name": str, "args": dict}
  {"type": "tool_result","name": str, "result": str}
  {"type": "plan_ready", "plan": dict}   ← TUI renders cleanup table
  {"type": "message",    "text": str}    ← Final LLM text response
  {"type": "error",      "text": str}
"""

from __future__ import annotations

import ast
import hashlib
import json
import time
from collections.abc import Generator
from typing import Optional

import litellm

from mac_cleaner.config import Settings
from mac_cleaner.tools import TOOLS, execute_tool

litellm.set_verbose = False  # suppress noisy debug output


def _origin_sig() -> str:
    """Return a 16-char provenance token stored in every audit entry."""
    return hashlib.sha256(b"PoofMac-Original-2026").hexdigest()[:16]


def _strip_provider_prefix(model: str) -> str:
    """``openai/nex-agi/x:free`` → ``nex-agi/x:free``, for display only."""
    for prefix in ("openai/", "openrouter/", "ollama/", "github_copilot/", "chatgpt/"):
        if model.startswith(prefix):
            return model[len(prefix):]
    return model


# LiteLLM wraps a provider's error body in its own exception text; these are the
# wrappers worth peeling off before showing the message to a user.
_ERROR_WRAPPERS = (
    "litellm.NotFoundError:",
    "litellm.BadRequestError:",
    "litellm.AuthenticationError:",
    "OpenAIException -",
    "NotFoundError:",
    "BadRequestError:",
    "AuthenticationError:",
)


def _endpoint_quote(raw: object) -> str:
    """The endpoint's own words, without LiteLLM's exception wrapper.

    A gateway 404 arrives as ``litellm.NotFoundError: OpenAIException -
    {'error': "The requested model … does not exist", …}``. Quoting that at the
    user reads like a crash, so unwrap it down to the sentence the endpoint
    actually wrote, and cap the length.
    """
    text = " ".join(str(raw or "").split())
    while True:
        for wrapper in _ERROR_WRAPPERS:
            if text.startswith(wrapper):
                text = text[len(wrapper):].lstrip()
                break
        else:
            break

    if "{" in text and "}" in text:
        body = text[text.find("{"): text.rfind("}") + 1]
        payload = None
        for loader in (json.loads, ast.literal_eval):
            try:
                payload = loader(body)
                break
            except (ValueError, SyntaxError):
                continue
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                text = error["message"]
            elif isinstance(error, str):
                text = error
            elif isinstance(payload.get("message"), str):
                text = payload["message"]
        text = " ".join(text.split())

    if len(text) > 240:
        text = text[:237].rstrip() + "…"
    return text

# ── Text-based tool call parser ───────────────────────────────────────────────

_KNOWN_TOOLS = {
    "get_disk_overview", "run_full_disk_scan", "scan_category",
    "check_path_safety", "propose_cleanup_plan",
}


def _extract_text_tool_calls(text: str) -> list[tuple[str, dict]]:
    """
    Parse tool calls that some models (Gemma, quantised variants) output as
    raw JSON in the message text instead of using the structured tool_calls field.

    Handles formats:
      {"name": "func", "arguments": {...}}
      {"function": "func", "arguments": {...}}
      [{"name": "func", "arguments": {...}}, ...]
      ```json\\n{...}\\n```
    """
    import re

    results: list[tuple[str, dict]] = []

    # Strip markdown code fences
    cleaned = re.sub(r"```(?:json)?\s*", "", text).replace("```", "").strip()

    # Try to find all JSON objects / arrays in the text
    candidates: list[str] = []

    # Look for top-level JSON blocks
    depth = 0
    start = -1
    for i, ch in enumerate(cleaned):
        if ch in ("{", "["):
            if depth == 0:
                start = i
            depth += 1
        elif ch in ("}", "]"):
            depth -= 1
            if depth == 0 and start != -1:
                candidates.append(cleaned[start : i + 1])
                start = -1

    for candidate in candidates:
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue

        # Normalise list of calls
        objs = obj if isinstance(obj, list) else [obj]

        for item in objs:
            if not isinstance(item, dict):
                continue
            # Extract function name from various key names
            fn_name = (
                item.get("name")
                or item.get("function")
                or item.get("tool")
                or item.get("function_name")
            )
            if not fn_name or fn_name not in _KNOWN_TOOLS:
                continue
            args = item.get("arguments") or item.get("args") or item.get("parameters") or {}
            if not isinstance(args, dict):
                args = {}
            results.append((fn_name, args))

    return results

# ── System prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are PoofMac, a disk-space analysis assistant for macOS.

YOUR ROLE
─────────
Help users reclaim disk space by analysing their Mac and producing a clear,
safe cleanup plan. You explain everything in plain developer-friendly language.

MANDATORY WORKFLOW — follow this EVERY time
────────────────────────────────────────────
1. Call get_disk_overview  →  understand current disk state.
2. Call run_full_disk_scan →  find everything recoverable.
3. Analyse results. For anything uncertain, call check_path_safety.
4. Call propose_cleanup_plan with ALL findings.
   - Include EVERY category found, even small ones.
   - Set risk_level accurately: SAFE / CAUTION / SKIP.
   - Write a clear "reason" for each item explaining what it is.

ABSOLUTE RULES — never break these
────────────────────────────────────
• You ONLY call the provided tools. You do NOT run shell commands or suggest
  the user run dangerous commands.
• NEVER propose deleting: /System, /usr, /bin, /etc, /Library (system),
  /Applications, ~/.ssh, ~/.aws, Keychain, Documents, Photos, Mail, Music,
  Movies, Desktop, Contacts, or any path you are not certain is a cache/temp.
• Set risk_level = SKIP for anything you are not confident is safe.
• Be honest. If you find very little to clean, say so.
• keep "reason" fields concise (1-2 sentences). Developers don't need essays.

RISK LEVELS
───────────
SAFE    — Auto-regenerated caches, logs, build artifacts. Confident = safe.
CAUTION — Downloads, build outputs, dev artifacts — user should review.
SKIP    — Anything system-critical, user data, or uncertain. Do not propose.
"""


# ── Agent ─────────────────────────────────────────────────────────────────────

class CleanerAgent:
    """
    Stateful agent that drives the LLM ↔ tools conversation loop.

    Usage
    ─────
    agent = CleanerAgent(settings)
    for event in agent.run("Analyse my disk and suggest what to clean"):
        # handle event dict
    """

    MAX_TURNS = 12  # Safety cap on agentic loop iterations

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.model, self.model_display = settings.get_active_model()
        self.messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
        self.cleanup_plan: Optional[dict] = None
        self._sig = _origin_sig()

    def _not_found_help(self, raw: str) -> str:
        """Explain a rejected model id in terms the user can act on.

        A gateway answers ``404 model_not_found`` with a wall of JSON, which
        reads like a crash. What actually happened is that the id we sent is
        not in the endpoint's catalogue — usually a stray vendor prefix or a
        ":free"-style suffix — so say that, and say where to fix it.
        """
        model_id = _strip_provider_prefix(self.model)
        if self.settings.endpoint_for(self.settings.get_active_provider()):
            advice = (
                "Model ids must match the endpoint exactly — an extra vendor prefix\n"
                "or a \":free\"-style suffix is enough for the gateway to reject it.\n"
                "Open Settings (⚙) → Custom endpoints, press Fetch models, and pick\n"
                "an id from the list."
            )
        else:
            advice = (
                "Pick the model again in Settings (⚙) — the provider does not offer\n"
                "the id that is currently selected."
            )
        lines = [f'The endpoint does not host "{model_id}" — {self.model_display}.', advice]
        quoted = _endpoint_quote(raw)
        if quoted:
            lines += ["", f"Endpoint said: {quoted}"]
        return "\n".join(lines)

    def run(self, user_message: str) -> Generator[dict, None, None]:
        """
        Drive the agent loop. Yields event dicts for the TUI to consume.
        Designed to run in a background thread.
        """
        self.messages.append({"role": "user", "content": user_message})
        turns = 0

        while turns < self.MAX_TURNS:
            turns += 1
            yield {"type": "status", "text": f"Thinking… ({self.model_display})"}

            # Provider extras (api_base/api_key for a custom OpenAI-compatible
            # endpoint; empty for every other provider). Re-read each turn so
            # Settings edited in the GUI apply to an already-running agent.
            extra_kwargs = self.settings.completion_kwargs()

            response = None
            for attempt in range(1, 4):  # up to 3 retries for transient errors
                try:
                    request_kwargs = {
                        "model": self.model,
                        "messages": self.messages,
                        "tools": TOOLS,
                        "tool_choice": "auto",
                        **extra_kwargs,
                    }
                    # ChatGPT subscription Codex models use LiteLLM's
                    # Responses bridge, which manages generation limits and
                    # does not accept chat-completions sampling parameters.
                    if not self.model.startswith("chatgpt/"):
                        request_kwargs["temperature"] = 0  # deterministic file analysis
                        request_kwargs["max_tokens"] = 4096
                    response = litellm.completion(**request_kwargs)
                    break  # success — exit retry loop
                except litellm.RateLimitError as exc:
                    yield {"type": "error", "text": f"Rate limit: {exc}. Wait a moment and retry."}
                    return
                except litellm.NotFoundError as exc:
                    yield {"type": "error", "text": self._not_found_help(str(exc))}
                    return
                except litellm.AuthenticationError as exc:
                    endpoint = self.settings.endpoint_for(self.settings.get_active_provider())
                    if endpoint is not None:
                        text = (
                            f"The endpoint rejected the API key for "
                            f"\"{endpoint.get('name') or 'Custom endpoint'}\".\n"
                            "Check the key in Settings (⚙) → Custom endpoints. "
                            "Leave it blank for a local server that needs none."
                        )
                    else:
                        text = (
                            "Authentication failed. Check the API key in "
                            "Settings (⚙)\n"
                            "Anthropic: https://console.anthropic.com\n"
                            "OpenRouter: https://openrouter.ai\n"
                            "OpenAI: https://platform.openai.com"
                        )
                    quoted = _endpoint_quote(exc)
                    yield {"type": "error", "text": f"{text}\n\n{quoted}" if quoted else text}
                    return
                except litellm.BadRequestError as exc:
                    text = str(exc)
                    lowered = text.lower()
                    if "model" in lowered and (
                        "not found" in lowered
                        or "does not exist" in lowered
                        or "model_not_found" in lowered
                    ):
                        yield {"type": "error", "text": self._not_found_help(text)}
                        return
                    yield {"type": "error", "text": f"Bad request: {text}"}
                    return
                except litellm.APIConnectionError as exc:
                    # Ollama cloud returns "Server overloaded" transiently
                    if attempt < 3:
                        wait = attempt * 8
                        yield {
                            "type": "status",
                            "text": f"Server busy — retrying in {wait}s… (attempt {attempt}/3)",
                        }
                        time.sleep(wait)
                    else:
                        yield {
                            "type": "error",
                            "text": (
                                f"Server unavailable after 3 attempts: {exc}\n"
                                "Ollama cloud may be under load. Try again in a minute."
                            ),
                        }
                        return
                except Exception as exc:  # noqa: BLE001
                    yield {"type": "error", "text": f"Unexpected error: {exc}"}
                    return

            if response is None:
                return

            message = response.choices[0].message

            # Store assistant message (convert to dict for JSON serialisability)
            self.messages.append(message.model_dump(exclude_none=True))

            # ── Structured tool calls (most models) ──────────────────────────
            tool_calls_to_run = []
            if message.tool_calls:
                for tc in message.tool_calls:
                    fn_name = tc.function.name
                    try:
                        fn_args = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError:
                        fn_args = {}
                    tool_calls_to_run.append((fn_name, fn_args, tc.id))

            # ── Text-based tool call fallback (Gemma, some quantised models) ─
            # Some models output tool calls as raw JSON in the text response
            # rather than using the structured tool_calls field.
            elif message.content:
                parsed = _extract_text_tool_calls(message.content)
                if parsed:
                    for fn_name, fn_args in parsed:
                        tool_calls_to_run.append((fn_name, fn_args, f"text_{fn_name}"))

            # ── Execute whatever tool calls we found ─────────────────────────
            if tool_calls_to_run:
                for fn_name, fn_args, call_id in tool_calls_to_run:
                    yield {"type": "tool_call", "name": fn_name, "args": fn_args}

                    tool_result = execute_tool(fn_name, fn_args)

                    yield {"type": "tool_result", "name": fn_name, "result": tool_result}

                    # Intercept cleanup plan — the TUI needs the raw args
                    if fn_name == "propose_cleanup_plan":
                        self.cleanup_plan = fn_args
                        yield {"type": "plan_ready", "plan": fn_args}

                    self.messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": tool_result,
                        }
                    )

                continue  # Get next LLM response after tool calls

            # ── Final text response ──────────────────────────────────────────
            if message.content:
                yield {"type": "message", "text": message.content}

            break  # No tool calls = agent is done

        if turns >= self.MAX_TURNS:
            yield {
                "type": "error",
                "text": "Agent reached max iterations without completing. Please retry.",
            }
