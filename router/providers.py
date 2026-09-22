"""Decision backends. Credentials and network calls stay outside ORCA's renderer.

Both backends answer the same rubric. The wording below is the LAYA `router`
preset verbatim (served by `GET /v1/presets` on the daemon); the coding
fine-tune depends on it, so change it only together with a new rubric id.
"""

import json
import math
import os
import urllib.error
import urllib.request

from credentials import read_jev_key

RUBRIC = "laya-router-v1"
LEVELS = ("trivial", "easy", "moderate", "hard")
DOMAINS = ("code", "math_or_logic", "writing", "factual_lookup", "data_analysis", "chitchat")
QUESTIONS = {
    "difficulty": {
        "type": "score",
        "instructions": "How hard is `request` for a language model?",
        "criteria": [
            "trivial: a lookup or one-liner",
            "easy: short answer, no reasoning",
            "moderate: several steps",
            "hard: long multi-step reasoning or specialist knowledge",
        ],
    },
    "domain": {
        "type": "choice",
        "instructions": "What domain does `request` belong to?",
        "criteria": {
            "code": "software engineering, programming, refactoring, architecture, debugging",
            "math_or_logic": "mathematics, logic puzzles, proofs, complex calculation",
            "writing": "creative writing, essays, emails, blog posts, copywriting",
            "factual_lookup": "facts, definitions, trivia, history",
            "data_analysis": "statistics, SQL, data manipulation, metrics",
            "chitchat": "casual conversation, greetings, small talk",
        },
    },
    "needs_tools": {
        "type": "noul",
        "instructions": "Does answering `request` require external tools, search or private data?",
    },
    "is_sensitive": {
        "type": "noul",
        "instructions": "Does `request` involve money, legal, medical or safety consequences?",
    },
}

DEFAULT_LAYA_URL = "http://127.0.0.1:8091"
JEV_URL = "https://api.typesafe.ai/v1/systemone"


class ProviderError(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ProviderError("Classifier redirected; configure its final endpoint explicitly")


def laya_url():
    return os.environ.get("LAYA_URL", DEFAULT_LAYA_URL).rstrip("/")


def get_json(url, timeout=5):
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=timeout) as response:
            return json.loads(response.read(1024 * 1024))
    except urllib.error.HTTPError as exc:
        raise ProviderError(f"Classifier returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ProviderError("Classifier unavailable or timed out") from exc
    except ValueError as exc:
        raise ProviderError("Classifier returned invalid JSON") from exc


def post_json(url, body, token=None, timeout=15):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, json.dumps(body).encode("utf-8"), headers)
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=timeout) as response:
            raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ProviderError("Classifier response is too large")
        return json.loads(raw)
    except urllib.error.HTTPError as exc:
        raise ProviderError(f"Classifier returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ProviderError("Classifier unavailable or timed out") from exc
    except ValueError as exc:
        raise ProviderError("Classifier returned invalid JSON") from exc


def probability(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProviderError("Classifier returned an invalid probability")
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ProviderError("Classifier returned an invalid probability")
    return float(value)


def normalize(result):
    """Turn a raw provider answer into routing signals; never substitutes zeros."""
    try:
        answers = result["answers"]
        difficulty = answers["difficulty"]
        if difficulty["type"] != "score" or answers["domain"]["type"] != "choice":
            raise ValueError("wrong answer type")
        distribution = {
            level: probability(difficulty["probabilities"][str(index)])
            for index, level in enumerate(LEVELS)
        }
        if abs(sum(distribution.values()) - 1) > 0.02:
            raise ValueError("distribution must sum to one")
        domain = answers["domain"]["choice"]
        if not isinstance(domain, str) or not domain:
            raise ValueError("missing domain")
        signals = {
            "difficulty": max(distribution, key=distribution.get),
            "difficulty_probabilities": distribution,
            "confidence": probability(difficulty["confidence"]),
            "domain": domain,
        }
        for key in ("needs_tools", "is_sensitive"):
            if answers[key]["type"] != "noul":
                raise ValueError("wrong answer type")
            signals[key] = probability(answers[key]["noul"])
        return signals
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ProviderError("Classifier response does not match the routing rubric") from exc


class Classifier:
    """Calls exactly one backend per request. No cross-backend fallback."""

    def __init__(self, request=post_json):
        self.request = request

    def predict(self, backend, prompt, language):
        if backend == "laya":
            # Two ways to ask the same rubric. `preset: router` relies on the
            # server registering that preset; inline `questions` works against
            # any LAYA-compatible endpoint and sends the identical body shape
            # Jev receives, with the prompt under `request` as the wording
            # expects. Field names change the answers, so keep them aligned.
            if os.environ.get("LAYA_SEND_QUESTIONS") == "1":
                body = {"state": {"request": prompt}, "questions": QUESTIONS}
            else:
                body = {"text": prompt, "preset": "router"}
            if language != "auto":
                body["lang"] = language
            result = self.request(laya_url() + "/v1/predict", body, os.environ.get("LAYA_API_KEY"))
            routing = result.get("routing") if isinstance(result, dict) else None
            model = routing.get("model", "unknown") if isinstance(routing, dict) else "unknown"
            model = f"laya/{model}"
        elif backend == "jev":
            token = read_jev_key()
            if not token:
                raise ProviderError("Connect Jev with agent-router configure jev, or set TYPESAFE_API_KEY")
            result = self.request(
                JEV_URL,
                {"model": os.environ.get("JEV_MODEL", "jev-1.13.0"),
                 "state": {"request": prompt}, "questions": QUESTIONS},
                token,
            )
            model = result.get("model", "unknown") if isinstance(result, dict) else "unknown"
            model = f"jev/{model}"
        else:
            raise ValueError("backend must be laya or jev")
        return {"signals": normalize(result), "model": model, "rubric": RUBRIC}

    def health(self, backend):
        """Reachability only; never sends prompt text."""
        if backend == "laya":
            body = get_json(laya_url() + "/health")
            if not isinstance(body, dict) or not body.get("ready"):
                raise ProviderError("LAYA is not ready")
            return {"backend": "laya", "model": body.get("model"), "loaded": body.get("loaded")}
        if backend == "jev":
            if not read_jev_key():
                raise ProviderError("Connect Jev with agent-router configure jev, or set TYPESAFE_API_KEY")
            return {"backend": "jev", "model": os.environ.get("JEV_MODEL", "jev-1.13.0"), "note": "credentials present; not probed"}
        raise ValueError("backend must be laya or jev")
