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
import re
import time
from collections.abc import Generator
from typing import Optional

import litellm

from mac_cleaner.config import MODEL_REGISTRY, Settings
from mac_cleaner.scanner import get_disk_usage
from mac_cleaner.tools import TOOLS, execute_tool


def _kilo_has_key() -> bool:
    """Whether a Kilo API key is currently configured."""
    try:
        return bool(Settings().kilo_api_key.strip())
    except Exception:  # noqa: BLE001 — advice must never crash the error path
        return False

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


# LiteLLM wraps a provider's error body in its own exception text, and it has
# an exception class per provider and status — so list them and one will always
# be missing (litellm.APIError was, and its name leaked into the chat). Peel
# wrappers off by shape instead: "litellm.APIError: APIError: OpenAIException -
# <the endpoint's sentence>".
_ERROR_WRAPPER_RE = re.compile(r"^(?:litellm\.)?[A-Za-z_]*(?:Error|Exception)\s*[:\-]\s*")


def _endpoint_quote(raw: object) -> str:
    """The endpoint's own words, without LiteLLM's exception wrapper.

    A gateway 404 arrives as ``litellm.NotFoundError: OpenAIException -
    {'error': "The requested model … does not exist", …}``. Quoting that at the
    user reads like a crash, so unwrap it down to the sentence the endpoint
    actually wrote, and cap the length.
    """
    text = " ".join(str(raw or "").split())
    while True:
        stripped = _ERROR_WRAPPER_RE.sub("", text, count=1)
        if stripped == text:
            break
        text = stripped.lstrip()

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


# LiteLLM raises a class per HTTP status, but the useful part is always the
# same: the endpoint's own sentence. These markers read guidance out of it, so
# even an unfamiliar status reads as English instead of "Unexpected error".
_MODEL_MISSING = (
    "does not exist",
    "model_not_found",
    "no such model",
    "unknown model",
    "invalid model",
    "model not found",
)
_CLIENT_REFUSED = (
    "only be used from within",
    "only available within",
    "must be used from",
    "only works from",
    "unauthorized client",
    "client is not allowed",
    "not allowed to use",
    "third-party client",
)
_AUTH_MARKERS = (
    "invalid api key",
    "incorrect api key",
    "invalid_api_key",
    "unauthorized",
    "authentication",
    "api key not valid",
    "no api key",
)
_CONNECTION_MARKERS = (
    "connection",
    "unreachable",
    "timed out",
    "timeout",
    "refused",
    "no such host",
    "certificate",
    "name or service not known",
)


def explain_provider_error(
    exc: object,
    *,
    model: str,
    display: str,
    is_custom_endpoint: bool,
) -> str:
    """Readable, actionable text for a failed provider request.

    One place decides what a provider failure means, so the chat and the
    settings page's Test connection button can never tell different stories.
    """
    quoted = _endpoint_quote(exc)
    lowered = quoted.lower()
    model_id = _strip_provider_prefix(model)

    auth_advice = (
        "Check the API key in Settings (⚙) → Custom endpoints. Leave it blank for a\n"
        "local server that needs none."
        if is_custom_endpoint
        else "Check the API key in Settings (⚙) — each cloud provider has its own\n"
        "page there."
    )
    if display.endswith("(Kilo Gateway)") and not _kilo_has_key():
        # Kilo's free ids need no key at all, so "check your API key" would
        # send the user hunting for a setting that is meant to stay empty.
        auth_advice = (
            "Kilo's free models need no API key — leave the key field empty in\n"
            "Settings (⚙). If a key is entered there, it is invalid: clear it and\n"
            "retry. (Free tiers are shared, so a busy moment can also answer with\n"
            "this error — trying again usually works.)"
        )

    if any(marker in lowered for marker in _MODEL_MISSING):
        lines = [f'The endpoint does not host "{model_id}" — {display}.']
        if is_custom_endpoint:
            lines.append(
                "Model ids must match the endpoint exactly — an extra vendor prefix\n"
                "or a \":free\"-style suffix is enough for the gateway to reject it.\n"
                "Open Settings (⚙) → Custom endpoints, press Fetch models, and pick\n"
                "an id from the list."
            )
        else:
            lines.append(
                "Pick the model again in Settings (⚙) — the provider does not offer\n"
                "the id that is currently selected."
            )
    elif any(marker in lowered for marker in _CLIENT_REFUSED):
        lines = [
            f"The endpoint refused the request — {display}.",
            "Services sometimes gate a free tier to their own app or command line.\n"
            "PoofMac cannot pass that check from the outside, and no setting here\n"
            "changes it: use an endpoint or API key that accepts external clients,\n"
            "or switch provider — GitHub Copilot or OpenAI Codex in Settings (⚙).",
        ]
    elif isinstance(exc, litellm.AuthenticationError) or any(
        marker in lowered for marker in _AUTH_MARKERS
    ):
        lines = [f"The endpoint rejected the credentials — {display}.", auth_advice]
    elif isinstance(exc, litellm.APIConnectionError) or any(
        marker in lowered for marker in _CONNECTION_MARKERS
    ):
        lines = [
            f"Could not reach the endpoint — {display}.",
            "Check that the server is running and that the Base URL is right, in\n"
            "Settings (⚙) → Custom endpoints.",
        ]
    else:
        lines = [
            f"The endpoint refused the request — {display}.",
            "Settings (⚙) → Custom endpoints shows the Base URL, key and model that\n"
            "were sent; Test connection reports which part is rejected.",
        ]

    if quoted:
        lines += ["", f"Endpoint said: {quoted}"]
    return "\n".join(lines)

# ── Text-based tool call parser ───────────────────────────────────────────────

_KNOWN_TOOLS = {
    "get_disk_overview", "run_full_disk_scan", "scan_category",
    "check_path_safety", "propose_cleanup_plan",
    # Maintenance & optimization
    "heavy_consumers", "hung_applications", "force_quit_app",
    "repair_applications", "app_acceleration", "launch_agents",
    "toggle_launch_agent", "memory_report", "tm_snapshots",
    "thin_tm_snapshots", "purgeable_space", "get_maintenance_guide",
    # App uninstaller
    "list_installed_apps", "find_app_residuals", "uninstall_app",
    # Large files
    "find_large_files",
    # Duplicates
    "find_duplicates",
    # Scheduled maintenance
    "maintenance_schedule",
    "get_maintenance_history",
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
You are PoofMac, a Mac maintenance assistant for macOS.

YOUR ROLE
─────────
You help with TWO kinds of requests:

1. DISK SPACE — analyse the Mac and produce a safe, clear cleanup plan.
2. MAINTENANCE & PERFORMANCE — slow Mac, memory, boot time, background
   services, hung apps, DNS or Spotlight trouble, Time Machine snapshots.

You are a CONVERSATION partner, not a batch job. Chat naturally:
• Answer questions directly and briefly. Small talk gets a small answer.
• If a request is vague ("my Mac is slow"), ask ONE short clarifying question
  ("Slow overall, or mainly at startup? Anything spinning in the Dock?")
  or investigate first with the read-only tools, then report what you see.
• You remember earlier turns in this conversation — build on them. Do not
  re-scan if you already have the answer from a moment ago.
• Give feedback as you go: after a tool result, say in one line what it
  showed before deciding the next step.
• End substantive answers with a short suggestion of what to do next
  ("Want me to thin those snapshots?"), but never nag.

DISK-SCAN WORKFLOW — only when the user wants disk space cleaned
─────────────────────────────────────────────────────────────────
1. Call get_disk_overview  →  understand current disk state.
2. Call run_full_disk_scan →  find everything recoverable.
3. For anything uncertain, call check_path_safety.
4. If you only need one category re-checked (a follow-up question about
   caches, logs, downloads…), call scan_category with that category name
   instead of re-running the whole scan.
5. Call propose_cleanup_plan with ALL findings.
   - Include EVERY category found, even small ones.
   - Set risk_level accurately: SAFE / CAUTION / SKIP.
   - Write a clear "reason" for each item explaining what it is.

MAINTENANCE TOOLS
─────────────────
• heavy_consumers — what is using CPU/RAM right now.
• hung_applications — GUI apps, suspected hangs and recorded freezes.
• force_quit_app — quit or kill a misbehaving app. ALWAYS ask the user first
  and warn that unsaved work is lost; use force=true only as a last resort.
• repair_applications — restart Finder/Dock/SystemUIServer (safe, instant
  relaunch) for stale icons, a frozen Dock or desktop glitches.
• app_acceleration — clear clipboard, recent lists, Finder/Dock state; warn
  that the clipboard will be lost. vacuum_mail=true compacts Mail's index.
• find_large_files — the biggest user files (installers, videos, archives).
  Read-only: report what you find with sizes and let the user decide; use
  check_path_safety before proposing any of them for deletion.
• find_duplicates — files with identical contents, grouped by reclaimable
  space, each group's oldest copy marked keep. Read-only. Report the groups
  and let the user choose; suggest the GUI's Duplicates tab, and use
  check_path_safety before proposing any copy for deletion.
• maintenance_schedule — install/remove/check the automatic maintenance
  timer (LaunchAgent running the safe set: acceleration, snapshot thinning,
  large-file report). Only enable or remove it when the user asks; never
  deletes anything.
• get_maintenance_history — what the timer did recently: runs, successes,
  snapshots reclaimed, largest files spotted. Read-only.
• list_installed_apps / find_app_residuals / uninstall_app — complete
  uninstalls: scan first, show the user the residual list, get their OK,
  then uninstall (Trash, recoverable). Keep preferences if they might
  reinstall. Never uninstall without an explicit confirmation.
• launch_agents / toggle_launch_agent — what runs at login; you may disable
  a USER agent, but always tell the user which one and why, and get their OK.
• memory_report — RAM pressure, purgeable memory, swap.
• tm_snapshots / thin_tm_snapshots — local snapshots eat disk; you may thin
  them after the user agrees.
• purgeable_space — what macOS could drop on demand.
• get_maintenance_guide — recipes for flush_dns_cache, reindex_spotlight,
  speed_up_boot, speed_up_mail, repair_disk_permissions.

STARTUP FACTS
─────────────
Every conversation ends this prompt with a LIVE FACTS block (current disk
usage and whether the maintenance timer is installed / how its last run went).
It is a snapshot from the moment the chat opened — it can go stale, so call
get_disk_overview before acting on it if the answer matters.

• Use it to be useful immediately: if the disk is nearly full, open with that
  instead of waiting to be asked; if the last scheduled run FAILED, say so in
  one line and offer to look into it.
• If the user enabled the timer, one short "your last run reclaimed X" is
  welcome; do not repeat it in every later reply.
• Never claim you ran a tool because a fact is in this block. It came from the
  app, not from a tool call.

ABSOLUTE RULES — never break these
──────────────────────────────────
• You ONLY call the provided tools. You NEVER run shell commands yourself.
• Maintenance steps that need admin rights (sudo): NEVER run them, never
  ask the user to paste blind commands. Use get_maintenance_guide, explain
  what each command does in one line, and let the user run it in Terminal.
• NEVER propose deleting: /System, /usr, /bin, /etc, /Library (system),
  /Applications, ~/.ssh, ~/.aws, Keychain, Documents, Photos, Mail, Music,
  Movies, Desktop, Contacts, or any path you are not certain is a cache/temp.
• Set risk_level = SKIP for anything you are not confident is safe.
• Be honest. If you find very little to clean, say so.
• Keep answers tight. Developers don't need essays.

RISK LEVELS
───────────
SAFE    — Auto-regenerated caches, logs, build artifacts. Confident = safe.
CAUTION — Downloads, build outputs, dev artifacts — user should review.
SKIP    — Anything system-critical, user data, or uncertain. Do not propose.
"""


def _startup_facts() -> str:
    """Live snapshot injected into the system prompt when a chat opens.

    Cheap local reads only (one ``statfs``, plus the schedule plist and its
    small JSON log) so opening a conversation stays instant and never blocks
    on the network.
    """
    lines: list[str] = []
    try:
        usage = get_disk_usage()
        lines.append(
            f"• Disk: {usage['used_percent']}% full — {usage['used_human']} of "
            f"{usage['total_human']} used, {usage['free_human']} free."
        )
    except Exception:  # noqa: BLE001 — a fact block must never break chat
        pass
    try:
        from mac_cleaner import scheduler  # local import: avoids an import cycle

        status = scheduler.schedule_status()
        runs = status["last_runs"]
        last = runs[0] if runs else None
        if status["installed"] and last:
            outcome = "ok" if last.get("success") else "FAILED"
            detail = f"{last.get('snapshots_thinned', 0)} snapshot(s) thinned"
            lines.append(
                f"• Scheduled maintenance: on, {status['interval_label']} — "
                f"last run {last.get('ran_at', 'unknown time')} ({outcome}, {detail})."
            )
        elif status["installed"]:
            lines.append(
                f"• Scheduled maintenance: on, {status['interval_label']} — "
                "no runs recorded yet."
            )
        elif last:
            # The timer was removed but its history is still on disk, and a
            # recent failure is exactly what the user needs told about.
            outcome = "ok" if last.get("success") else "FAILED"
            lines.append(
                f"• Scheduled maintenance: not installed — last run "
                f"{last.get('ran_at', 'unknown time')} ({outcome})."
            )
        else:
            lines.append("• Scheduled maintenance: not installed.")
    except Exception:  # noqa: BLE001
        pass
    return "\n".join(lines)


def _build_system_prompt() -> str:
    """SYSTEM_PROMPT plus a LIVE FACTS snapshot taken right now.

    Assembled once per conversation (CleanerAgent.__init__): the facts are a
    snapshot, and re-reading them mid-conversation would invite the model to
    talk about a state the user never saw change.
    """
    facts = _startup_facts()
    if not facts:
        return SYSTEM_PROMPT
    stamp = time.strftime("%Y-%m-%d %H:%M")
    return f"{SYSTEM_PROMPT}\n\nLIVE FACTS — snapshot at {stamp}\n{facts}\n"


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
        self.messages: list[dict] = [
            {"role": "system", "content": _build_system_prompt()}
        ]
        self.cleanup_plan: Optional[dict] = None
        self._sig = _origin_sig()

    def _explain(self, exc: object) -> str:
        """The message the user sees for any failed request from this agent."""
        return explain_provider_error(
            exc,
            model=self.model,
            display=self.model_display,
            is_custom_endpoint=bool(
                self.settings.endpoint_for(self.settings.get_active_provider())
            ),
        )

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
                    yield {
                        "type": "error",
                        "text": f"Rate limit: {_endpoint_quote(exc)}. Wait a moment and retry.",
                    }
                    return
                except litellm.NotFoundError as exc:
                    yield {"type": "error", "text": self._explain(exc)}
                    return
                except litellm.AuthenticationError as exc:
                    yield {"type": "error", "text": self._explain(exc)}
                    return
                except litellm.BadRequestError as exc:
                    yield {"type": "error", "text": self._explain(exc)}
                    return
                except litellm.APIError as exc:
                    # Every other status the provider can return — a refused
                    # free tier, a proxy in the way, a 500 — used to fall
                    # through to "Unexpected error: litellm.APIError: …".
                    yield {"type": "error", "text": self._explain(exc)}
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
                                f"Server unavailable after 3 attempts: "
                                f"{_endpoint_quote(exc)}\n"
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
