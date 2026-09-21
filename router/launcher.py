"""Approved dispatch through ORCA's own runtime. One confirmation, one launch, one log line."""

import json
import os
import shlex
import shutil
import sys
import subprocess
import time

from routing import prompt_digest

APP_NAME = "orca-agent-router"

# Order matters. On macOS `which orca` finds /usr/local/bin/orca, a root-owned
# symlink whose launcher script cannot resolve its own app path ("Unable to
# determine Orca.app path from symlink"), so the bundle path is tried first.
ORCA_CANDIDATES = (
    os.environ.get("ORCA_CLI", ""),
    "/Applications/Orca.app/Contents/Resources/bin/orca",
    os.path.join(os.path.expanduser("~"), "Applications/Orca.app/Contents/Resources/bin/orca"),
    shutil.which("orca") or "",
)
SEND_WAIT_SECONDS = 15
BOOT_TIMEOUT_MS = 20000


class LaunchError(RuntimeError):
    pass


def orca_binary():
    for candidate in ORCA_CANDIDATES:
        if candidate and os.path.exists(candidate) and os.access(candidate, os.X_OK):
            return candidate
    raise LaunchError("ORCA CLI not found; set ORCA_CLI to Orca.app/Contents/Resources/bin/orca")


def harness_command(route):
    """Model selection per harness, from the route only; ORCA's --model covers fewer harnesses."""
    harness, model, provider = route["harness"], route["model"], route["provider"]
    if harness == "claude":
        argv = ["claude", "--model", model]
    elif harness == "codex":
        argv = ["codex", "-m", model]
    elif harness == "opencode":
        argv = ["opencode", "--model", model]
    elif harness == "goose":
        argv = ["goose", "session", "--provider", provider, "--model", model]
    else:
        raise LaunchError(f"Unsupported harness {harness}")
    return " ".join(shlex.quote(part) for part in argv + list(route.get("args", [])))


def state_dir():
    """Where this tool keeps its own files. Depends on no particular project."""
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Application Support", APP_NAME)
    if os.name == "nt":
        return os.path.join(os.environ.get("LOCALAPPDATA") or home, APP_NAME)
    return os.path.join(
        os.environ.get("XDG_STATE_HOME") or os.path.join(home, ".local", "state"), APP_NAME
    )


def decisions_log_path():
    configured = os.environ.get("AGENT_ROUTER_LOG")
    if configured:
        return configured
    return os.path.join(state_dir(), "decisions.jsonl")


def record(entry):
    path = decisions_log_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as sink:
        sink.write(json.dumps({"at": time.time(), **entry}, ensure_ascii=False) + "\n")


def find_handle(payload):
    if isinstance(payload, dict):
        handle = payload.get("handle")
        if isinstance(handle, str) and handle.startswith("term_"):
            return handle
        for value in payload.values():
            found = find_handle(value)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = find_handle(value)
            if found:
                return found
    return None


def run_orca(argv, runner=subprocess.run):
    completed = runner([orca_binary(), *argv, "--json"], capture_output=True, text=True, timeout=120)
    try:
        payload = json.loads(completed.stdout or "{}")
    except ValueError as exc:
        raise LaunchError(f"ORCA returned non-JSON output: {completed.stderr.strip()[:400]}") from exc
    if completed.returncode != 0 or not payload.get("ok", False):
        detail = payload.get("error") or completed.stderr.strip() or completed.stdout.strip()
        raise LaunchError(f"orca {' '.join(argv[:2])} failed: {str(detail)[:400]}")
    return payload.get("result", payload)


def launch(proposal, route, prompt, worktree="active", dry_run=False, runner=subprocess.run):
    """Bind prompt + route to the proposal, then create the agent terminal and send the prompt."""
    if prompt_digest(prompt) != proposal["prompt_sha256"]:
        raise LaunchError("Prompt changed since the proposal; request a new proposal")
    if route["id"] not in {c["id"] for c in proposal["candidates"]}:
        raise LaunchError("Route is not one of the proposal's candidates; request a new proposal")
    command = harness_command(route)
    plan = {
        "proposal_id": proposal["proposal_id"],
        "prompt_sha256": proposal["prompt_sha256"],
        "backend": proposal["backend"],
        "classifier_model": proposal["classifier_model"],
        "rubric": proposal["rubric"],
        "recommended_route_id": proposal["recommended_route_id"],
        "route_id": route["id"],
        "override": route["id"] != proposal["recommended_route_id"],
        "harness": route["harness"],
        "model": route["model"],
        "command": command,
        "worktree": worktree,
    }
    if dry_run:
        return {**plan, "dry_run": True}
    created = run_orca(["terminal", "create", "--worktree", worktree, "--title", route["label"], "--command", command], runner)
    handle = find_handle(created)
    if not handle:
        raise LaunchError("ORCA created a terminal but returned no handle")
    # Let the TUI reach its input box before typing; timeout is not fatal (the send reports acceptance).
    try:
        run_orca(["terminal", "wait", "--terminal", handle, "--for", "tui-idle", "--timeout-ms", str(BOOT_TIMEOUT_MS)], runner)
    except LaunchError:
        pass
    sent = run_orca(["terminal", "send", "--terminal", handle, "--text", prompt, "--enter",
                     "--wait-submit", str(SEND_WAIT_SECONDS)], runner)
    outcome = {**plan, "terminal": handle, "send": sent}
    record(outcome)
    return outcome
