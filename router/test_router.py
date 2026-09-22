"""Stdlib tests: python3 -m unittest -v (from src/bin/agent-router)."""

import base64
import json
import os
import shlex
import tempfile
import unittest
from unittest import mock

import cli
import launcher
import providers
import routing

LAYA_RESPONSE = {
    "model": "laya-rl-agent",
    "answers": {
        "difficulty": {"type": "score", "score": 1.96,
                       "probabilities": {"0": 0.0071, "1": 0.1649, "2": 0.6916, "3": 0.1363}, "confidence": 0.3802},
        "domain": {"type": "choice", "choice": "code", "confidence": 0.871},
        "needs_tools": {"type": "noul", "noul": 0.0947, "confidence": 0.9053},
        "is_sensitive": {"type": "noul", "noul": 0.0916, "confidence": 0.9084},
    },
    "routing": {"model": "english"},
    "latency_ms": 189.7, "preset": "router",
}

ROUTES = [
    {"id": "fast", "label": "Claude Haiku", "harness": "claude", "provider": "anthropic", "model": "haiku",
     "levels": ["trivial", "easy"], "local": False, "tools": True},
    {"id": "std", "label": "Claude Sonnet", "harness": "claude", "provider": "anthropic", "model": "sonnet",
     "levels": ["moderate"], "local": False, "tools": True},
    {"id": "local", "label": "Goose local", "harness": "goose", "provider": "openai", "model": "qwen",
     "levels": ["trivial", "easy", "moderate"], "local": True, "tools": False},
    {"id": "codex", "label": "Codex", "harness": "codex", "provider": "openai", "model": "gpt-5-codex",
     "levels": ["moderate"], "domains": ["writing"], "local": False, "tools": True},
]


def fake_request(response=LAYA_RESPONSE):
    calls = []

    def request(url, body, token=None, timeout=15):
        calls.append({"url": url, "body": body, "token": token})
        return json.loads(json.dumps(response))

    request.calls = calls
    return request


def request_body(**overrides):
    body = {"prompt": "Fix the race condition without changing the API", "backend": "laya",
            "language": "auto", "allowed_harnesses": ["claude", "codex", "goose"], "local_only": False}
    body.update(overrides)
    return body


class NormalizeTests(unittest.TestCase):
    def test_laya_shape_normalizes(self):
        signals = providers.normalize(LAYA_RESPONSE)
        self.assertEqual(signals["difficulty"], "moderate")
        self.assertEqual(signals["domain"], "code")
        self.assertAlmostEqual(signals["needs_tools"], 0.0947)
        self.assertAlmostEqual(sum(signals["difficulty_probabilities"].values()), 1, places=2)

    def test_missing_answer_is_failure_not_zero(self):
        broken = json.loads(json.dumps(LAYA_RESPONSE))
        del broken["answers"]["needs_tools"]
        with self.assertRaises(providers.ProviderError):
            providers.normalize(broken)

    def test_bad_distribution_rejected(self):
        broken = json.loads(json.dumps(LAYA_RESPONSE))
        broken["answers"]["difficulty"]["probabilities"]["3"] = 0.9
        with self.assertRaises(providers.ProviderError):
            providers.normalize(broken)

    def test_rubric_matches_laya_router_preset_shape(self):
        self.assertEqual(set(providers.QUESTIONS), {"difficulty", "domain", "needs_tools", "is_sensitive"})
        self.assertEqual(tuple(providers.QUESTIONS["domain"]["criteria"]), providers.DOMAINS)
        self.assertEqual(len(providers.QUESTIONS["difficulty"]["criteria"]), len(providers.LEVELS))


class ClassifierTests(unittest.TestCase):
    def test_laya_only_calls_laya_and_passes_lang(self):
        request = fake_request()
        with mock.patch.dict(os.environ, {"LAYA_URL": "http://127.0.0.1:8091/"}, clear=False):
            prediction = providers.Classifier(request).predict("laya", "Explique cette fonction", "fr")
        self.assertEqual(len(request.calls), 1)
        self.assertEqual(request.calls[0]["url"], "http://127.0.0.1:8091/v1/predict")
        self.assertEqual(request.calls[0]["body"], {"text": "Explique cette fonction", "preset": "router", "lang": "fr"})
        self.assertEqual(prediction["model"], "laya/english")
        self.assertEqual(prediction["rubric"], providers.RUBRIC)

    def test_laya_inline_questions_mode_matches_the_jev_body(self):
        """Bring-your-own-LAYA: no server-side preset needed, same rubric, same field."""
        request = fake_request()
        with mock.patch.dict(os.environ, {"LAYA_SEND_QUESTIONS": "1"}, clear=False):
            providers.Classifier(request).predict("laya", "Explique cette fonction", "fr")
        body = request.calls[0]["body"]
        self.assertEqual(body["state"], {"request": "Explique cette fonction"})
        self.assertIs(body["questions"], providers.QUESTIONS)
        self.assertEqual(body["lang"], "fr")
        self.assertNotIn("preset", body)
        self.assertNotIn("text", body)

    def test_jev_without_key_fails_before_any_call(self):
        request = fake_request()
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch("providers.read_jev_key", return_value=None):
            with self.assertRaises(providers.ProviderError):
                providers.Classifier(request).predict("jev", "hi", "auto")
        self.assertEqual(request.calls, [])

    def test_jev_sends_same_rubric_with_request_field(self):
        request = fake_request()
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "k"}, clear=False):
            providers.Classifier(request).predict("jev", "Explain this function", "en")
        call = request.calls[0]
        self.assertEqual(call["url"], providers.JEV_URL)
        self.assertEqual(call["token"], "k")
        self.assertEqual(call["body"]["state"], {"request": "Explain this function"})
        self.assertIs(call["body"]["questions"], providers.QUESTIONS)


class RouterTests(unittest.TestCase):
    def router(self, response=LAYA_RESPONSE):
        return routing.Router(json.loads(json.dumps(ROUTES)), providers.Classifier(fake_request(response)))

    def test_recommends_matching_level_and_keeps_alternatives(self):
        proposal = self.router().propose(request_body())
        self.assertEqual(proposal["recommended_route_id"], "std")
        ids = [c["id"] for c in proposal["candidates"]]
        self.assertEqual(ids[0], "std")
        self.assertIn("fast", ids)
        self.assertNotIn("codex", [c["id"] for c in proposal["candidates"] if c["matches"]])  # domain mismatch
        self.assertTrue(proposal["requires_confirmation"])
        self.assertEqual(len(proposal["prompt_sha256"]), 64)

    def test_tools_need_filters_toolless_routes(self):
        response = json.loads(json.dumps(LAYA_RESPONSE))
        response["answers"]["needs_tools"]["noul"] = 0.8
        proposal = self.router(response).propose(request_body())
        self.assertNotIn("local", [c["id"] for c in proposal["candidates"]])

    def test_sensitive_prefers_local_among_matches(self):
        response = json.loads(json.dumps(LAYA_RESPONSE))
        response["answers"]["is_sensitive"]["noul"] = 0.9
        proposal = self.router(response).propose(request_body())
        self.assertEqual(proposal["recommended_route_id"], "local")

    def test_local_only_rejects_jev_and_cloud_routes(self):
        with self.assertRaises(ValueError):
            self.router().propose(request_body(local_only=True, backend="jev"))
        proposal = self.router().propose(request_body(local_only=True))
        self.assertEqual([c["id"] for c in proposal["candidates"]], ["local"])

    def test_no_match_means_manual_selection(self):
        response = json.loads(json.dumps(LAYA_RESPONSE))
        response["answers"]["difficulty"]["probabilities"] = {"0": 0.01, "1": 0.01, "2": 0.08, "3": 0.9}
        proposal = self.router(response).propose(request_body())
        self.assertIsNone(proposal["recommended_route_id"])
        self.assertTrue(proposal["manual_selection"])

    def test_laya_prompt_byte_budget(self):
        with self.assertRaises(ValueError):
            self.router().propose(request_body(prompt="é" * 600))

    def test_route_validation(self):
        bad = json.loads(json.dumps(ROUTES))
        bad[0]["model"] = "--dangerous"
        with self.assertRaises(ValueError):
            routing.validate_routes(bad)
        bad = json.loads(json.dumps(ROUTES))
        bad.append({**bad[0], "id": "oc", "harness": "opencode", "model": "nomodelslash"})
        with self.assertRaises(ValueError):
            routing.validate_routes(bad)


class LauncherTests(unittest.TestCase):
    def proposal(self):
        return routing.Router(json.loads(json.dumps(ROUTES)), providers.Classifier(fake_request())).propose(request_body())

    def test_harness_commands(self):
        self.assertEqual(launcher.harness_command(ROUTES[0]), "claude --model haiku")
        self.assertEqual(launcher.harness_command(ROUTES[3]), "codex -m gpt-5-codex")
        self.assertEqual(launcher.harness_command(ROUTES[2]), "goose session --provider openai --model qwen")
        self.assertEqual(launcher.harness_command({**ROUTES[0], "args": ["--permission-mode", "plan it"]}),
                         "claude --model haiku --permission-mode 'plan it'")

    def test_dry_run_binds_prompt_and_route(self):
        proposal = self.proposal()
        route = proposal["candidates"][0]
        plan = launcher.launch(proposal, route, request_body()["prompt"], dry_run=True)
        self.assertTrue(plan["dry_run"])
        self.assertFalse(plan["override"])
        with self.assertRaises(launcher.LaunchError):
            launcher.launch(proposal, route, "a different prompt", dry_run=True)
        with self.assertRaises(launcher.LaunchError):
            launcher.launch(proposal, {**route, "id": "nope"}, request_body()["prompt"], dry_run=True)

    def test_launch_sends_prompt_unchanged_and_logs(self):
        proposal = self.proposal()
        route = next(c for c in proposal["candidates"] if c["id"] == "fast")
        prompt = "Corrige la condition de concurrence sans modifier l'API — `retry()` ≠ ok"
        proposal["prompt_sha256"] = routing.prompt_digest(prompt)
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            verb = argv[2]
            payload = {"ok": True, "result": {"terminal": {"handle": "term_1"}} if verb == "create" else {"accepted": True}}
            return mock.Mock(returncode=0, stdout=json.dumps(payload), stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, "d.jsonl")
            with mock.patch.dict(os.environ, {"AGENT_ROUTER_LOG": log, "ORCA_CLI": "/bin/sh"}, clear=False):
                result = launcher.launch(proposal, route, prompt, runner=runner)
            with open(log, encoding="utf-8") as source:
                logged = json.loads(source.readline())
        self.assertEqual([c[1:3] for c in calls], [["terminal", "create"], ["terminal", "wait"], ["terminal", "send"]])
        create = calls[0]
        self.assertEqual(create[create.index("--command") + 1], "claude --model haiku")
        send = calls[2]
        self.assertEqual(send[send.index("--text") + 1], prompt)
        self.assertIn("--enter", send)
        self.assertEqual(result["terminal"], "term_1")
        self.assertTrue(logged["override"])
        self.assertEqual(logged["route_id"], "fast")
        self.assertNotIn("prompt", logged)

    def test_orca_failure_is_reported_not_retried(self):
        proposal = self.proposal()

        def runner(argv, **kwargs):
            return mock.Mock(returncode=1, stdout=json.dumps({"ok": False, "error": "no active worktree"}), stderr="")

        with mock.patch.dict(os.environ, {"ORCA_CLI": "/bin/sh"}, clear=False):
            with self.assertRaises(launcher.LaunchError):
                launcher.launch(proposal, proposal["candidates"][0], request_body()["prompt"], runner=runner)


class CliTests(unittest.TestCase):
    def test_b64_payload_roundtrip(self):
        body = request_body(prompt="Explique cette fonction — «accents» préservés")
        encoded = base64.urlsafe_b64encode(json.dumps(body, ensure_ascii=False).encode("utf-8")).decode("ascii").rstrip("=")
        self.assertEqual(cli.decode_payload(encoded), body)
        with self.assertRaises(ValueError):
            cli.decode_payload("not*base64")

    def test_propose_only_json(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(ROUTES, handle)
        try:
            with mock.patch.object(providers.Classifier, "predict",
                                   return_value={"signals": providers.normalize(LAYA_RESPONSE), "model": "laya/english", "rubric": providers.RUBRIC}):
                with mock.patch("sys.stdout") as out:
                    code = cli.main(["route", "--prompt", "Explain this function", "--harness", "claude",
                                     "--routes", handle.name, "--propose-only", "--json"])
            self.assertEqual(code, 0)
            printed = "".join(str(c.args[0]) for c in out.write.call_args_list)
            self.assertIn('"recommended_route_id": "std"', printed)
        finally:
            os.unlink(handle.name)

    def test_classifier_error_exit_code(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(ROUTES, handle)
        try:
            with mock.patch.object(providers.Classifier, "predict", side_effect=providers.ProviderError("down")):
                code = cli.main(["route", "--prompt", "hi", "--harness", "claude", "--routes", handle.name])
            self.assertEqual(code, 3)
        finally:
            os.unlink(handle.name)


if __name__ == "__main__":
    unittest.main()
