"""Configured route selection; never executes an agent or infers account access."""

import hashlib
import json
import os
import time
import uuid

from providers import Classifier, DOMAINS, LEVELS

# Harness ids ORCA recognizes for launch (`orca terminal create --command`).
HARNESSES = ("claude", "codex", "opencode", "goose")
MAX_PROMPT_CHARS = 32000
# LAYA has not exposed tokenization yet; refuse rather than classify a clipped prompt.
LAYA_MAX_PROMPT_BYTES = 1000


def nonempty(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def cli_safe(value, name):
    """Route fields become argv entries; reject flag-shaped and control text."""
    if value.startswith("-") or any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError(f"Invalid route {name}")
    return value


def validate_routes(routes):
    if not isinstance(routes, list) or not 1 <= len(routes) <= 64:
        raise ValueError("Configure between 1 and 64 routes")
    seen = set()
    for route in routes:
        if not isinstance(route, dict):
            raise ValueError("Each route must be an object")
        for key in ("id", "label", "harness", "provider", "model"):
            nonempty(route.get(key), key)
        if route["id"] in seen or route["harness"] not in HARNESSES:
            raise ValueError("Route IDs must be unique and harnesses supported")
        seen.add(route["id"])
        levels = route.get("levels")
        if not isinstance(levels, list) or not levels or any(level not in LEVELS for level in levels):
            raise ValueError("Each route needs valid difficulty levels")
        domains = route.get("domains", [])
        if not isinstance(domains, list) or any(domain not in DOMAINS for domain in domains):
            raise ValueError("Route domains must come from the rubric")
        route["domains"] = domains
        if not isinstance(route.get("local"), bool) or not isinstance(route.get("tools"), bool):
            raise ValueError("Each route needs explicit local and tools booleans")
        for key in ("provider", "model"):
            cli_safe(route[key], key)
        if route["harness"] == "opencode" and "/" not in route["model"]:
            raise ValueError("opencode models are written provider/model")
        args = route.get("args", [])
        if not isinstance(args, list) or any(not isinstance(a, str) or not a or any(ord(c) < 32 for c in a) for a in args):
            raise ValueError("Route args must be a list of plain strings")
        route["args"] = args
    return routes


def default_routes_path():
    configured = os.environ.get("AGENT_ROUTER_ROUTES")
    if configured:
        return configured
    here = os.path.dirname(os.path.abspath(__file__))
    for name in ("routes.json", "routes.example.json"):
        candidate = os.path.join(here, name)
        if os.path.exists(candidate):
            return candidate
    raise FileNotFoundError("No routes.json; copy routes.example.json or set AGENT_ROUTER_ROUTES")


def load_routes(path=None):
    with open(path or default_routes_path(), encoding="utf-8") as source:
        return validate_routes(json.load(source))


def prompt_digest(prompt):
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


class Router:
    def __init__(self, routes, classifier=None):
        self.routes = validate_routes(routes)
        self.classifier = classifier or Classifier()

    def validate_request(self, body):
        if not isinstance(body, dict):
            raise ValueError("Request must be an object")
        prompt = nonempty(body.get("prompt"), "prompt")
        if len(prompt) > MAX_PROMPT_CHARS:
            raise ValueError(f"Prompt exceeds the router's {MAX_PROMPT_CHARS:,}-character limit")
        backend = body.get("backend", "laya")
        if backend not in ("laya", "jev"):
            raise ValueError("backend must be laya or jev")
        language = body.get("language", "auto")
        if language not in ("auto", "en", "fr"):
            raise ValueError("language must be auto, en or fr")
        allowed = body.get("allowed_harnesses")
        if not isinstance(allowed, list) or not allowed or any(name not in HARNESSES for name in allowed):
            raise ValueError("allowed_harnesses must list supported harnesses")
        local_only = body.get("local_only", False)
        if not isinstance(local_only, bool):
            raise ValueError("local_only must be a boolean")
        if local_only and backend == "jev":
            raise ValueError("Local-only requests cannot use hosted Jev")
        if backend == "laya" and len(prompt.encode("utf-8")) > LAYA_MAX_PROMPT_BYTES:
            raise ValueError(
                f"LAYA routing currently accepts up to {LAYA_MAX_PROMPT_BYTES:,} UTF-8 bytes; shorten the prompt or select Jev"
            )
        return prompt, backend, language, allowed, local_only

    def eligible(self, allowed, local_only):
        eligible = [r for r in self.routes if r["harness"] in allowed and (r["local"] or not local_only)]
        if not eligible:
            raise ValueError("No configured route matches the available harnesses and constraints")
        return eligible

    def rank(self, eligible, signals):
        """Deterministic rules only; reasons are rule text, never model prose."""
        sensitive = signals["is_sensitive"] >= 0.5
        candidates = []
        for route in eligible:
            reasons = []
            if signals["needs_tools"] >= 0.5 and not route["tools"]:
                continue
            level_match = signals["difficulty"] in route["levels"]
            reasons.append(
                ("Configured for " if level_match else "Not configured for ")
                + f"{signals['difficulty']} tasks ({', '.join(route['levels'])})"
            )
            domain_match = not route["domains"] or signals["domain"] in route["domains"]
            if route["domains"]:
                reasons.append(("Covers " if domain_match else "Does not cover ") + f"domain {signals['domain']}")
            if sensitive and route["local"]:
                reasons.append("Sensitive prompt; local route preferred")
            matches = level_match and domain_match
            candidates.append({**route, "matches": matches, "reasons": reasons,
                               "sort": (not matches, not (sensitive and route["local"]))})
        candidates.sort(key=lambda route: route["sort"])
        for route in candidates:
            del route["sort"]
        return candidates

    def propose(self, body):
        prompt, backend, language, allowed, local_only = self.validate_request(body)
        eligible = self.eligible(allowed, local_only)
        started = time.monotonic()
        prediction = self.classifier.predict(backend, prompt, language)
        signals = prediction["signals"]
        candidates = self.rank(eligible, signals)
        recommended = next((r["id"] for r in candidates if r["matches"]), None)
        return {
            "proposal_id": str(uuid.uuid4()),
            "prompt_sha256": prompt_digest(prompt),
            "backend": backend, "classifier_model": prediction["model"],
            "rubric": prediction["rubric"], "language": language, "local_only": local_only,
            "signals": signals, "recommended_route_id": recommended,
            "candidates": candidates, "requires_confirmation": True,
            "manual_selection": recommended is None,
            "latency_ms": round((time.monotonic() - started) * 1000),
        }
