#!/usr/bin/env python3
"""Grade a prompt with LAYA or Jev, propose a harness/model route, launch only after approval.

    agent-router route --prompt "Fix the race without changing the API"
    agent-router route --prompt-file task.md --backend jev --harness claude --harness codex
    agent-router route --b64 <payload>        # what the ORCA panel types into a terminal
    agent-router health [--backend laya|jev]
"""

import argparse
import base64
import json
import os
import shutil
import sys

from launcher import LaunchError, launch, record
from providers import Classifier, ProviderError
from routing import HARNESSES, Router, load_routes


def installed_harnesses():
    return [name for name in HARNESSES if shutil.which(name)]


def decode_payload(encoded):
    padded = encoded + "=" * (-len(encoded) % 4)
    try:
        body = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, UnicodeError) as exc:
        raise ValueError("--b64 payload is not base64url JSON") from exc
    if not isinstance(body, dict):
        raise ValueError("--b64 payload must be an object")
    return body


def read_prompt(args):
    if args.b64:
        body = decode_payload(args.b64)
        body.setdefault("backend", args.backend)
        body.setdefault("language", args.language)
        body.setdefault("local_only", args.local_only)
        body.setdefault("allowed_harnesses", args.harness or installed_harnesses())
        return body
    if args.prompt_file:
        with open(args.prompt_file, encoding="utf-8") as source:
            prompt = source.read()
    elif args.prompt is not None:
        prompt = args.prompt
    elif not sys.stdin.isatty():
        prompt = sys.stdin.read()
    else:
        raise ValueError("Give the prompt with --prompt, --prompt-file, --b64 or on stdin")
    return {
        "prompt": prompt.strip("\n"),
        "backend": args.backend,
        "language": args.language,
        "local_only": args.local_only,
        "allowed_harnesses": args.harness or installed_harnesses(),
    }


def print_proposal(proposal, out=sys.stdout):
    signals = proposal["signals"]
    dist = " ".join(f"{k[:4]}={v:.2f}" for k, v in signals["difficulty_probabilities"].items())
    print(f"classifier  {proposal['backend']} ({proposal['classifier_model']}), rubric {proposal['rubric']}, "
          f"{proposal['latency_ms']} ms", file=out)
    print(f"difficulty  {signals['difficulty']}  [{dist}]  confidence {signals['confidence']:.2f}", file=out)
    print(f"domain      {signals['domain']}   needs_tools {signals['needs_tools']:.2f}   "
          f"is_sensitive {signals['is_sensitive']:.2f}", file=out)
    print(file=out)
    for index, route in enumerate(proposal["candidates"], 1):
        marker = "*" if route["id"] == proposal["recommended_route_id"] else " "
        scope = "local" if route["local"] else "cloud"
        print(f"{marker}{index:>2}. {route['label']:<28} {route['harness']}/{route['model']:<24} {scope}", file=out)
        for reason in route["reasons"]:
            print(f"      - {reason}", file=out)
    if proposal["manual_selection"]:
        print("\nNo route matches the classification; pick one manually or quit.", file=out)


def choose(proposal, assume_yes):
    candidates = proposal["candidates"]
    if assume_yes:
        if proposal["manual_selection"]:
            raise LaunchError("--yes refused: no recommended route; choose interactively")
        return next(c for c in candidates if c["id"] == proposal["recommended_route_id"])
    if not sys.stdin.isatty():
        raise LaunchError("Approval needs a terminal; rerun interactively or pass --yes")
    default = next((i for i, c in enumerate(candidates, 1) if c["id"] == proposal["recommended_route_id"]), None)
    hint = f" [{default}]" if default else ""
    while True:
        answer = input(f"\nLaunch route 1-{len(candidates)}{hint}, or q to quit: ").strip().lower()
        if answer in ("q", "quit", ""):
            if answer == "" and default:
                return candidates[default - 1]
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(candidates):
            return candidates[int(answer) - 1]
        print("Type a listed number or q.")


def cmd_route(args):
    body = read_prompt(args)
    router = Router(load_routes(args.routes))
    proposal = router.propose(body)
    if args.json:
        print(json.dumps(proposal, ensure_ascii=False, indent=2))
    else:
        print_proposal(proposal)
    if args.propose_only:
        return 0
    route = choose(proposal, args.yes)
    if route is None:
        record({"proposal_id": proposal["proposal_id"], "prompt_sha256": proposal["prompt_sha256"],
                "backend": proposal["backend"], "recommended_route_id": proposal["recommended_route_id"],
                "route_id": None, "declined": True})
        print("Nothing launched.")
        return 0
    result = launch(proposal, route, body["prompt"], worktree=args.worktree, dry_run=args.dry_run)
    if args.json or args.dry_run:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Launched {route['label']} in terminal {result['terminal']} ({result['command']})")
    return 0


def cmd_health(args):
    classifier = Classifier()
    status = 0
    for backend in ([args.backend] if args.backend else ["laya", "jev"]):
        try:
            print(json.dumps(classifier.health(backend)))
        except (ProviderError, ValueError) as exc:
            print(json.dumps({"backend": backend, "error": str(exc)}))
            status = 1 if args.backend else status
    return status


def build_parser():
    parser = argparse.ArgumentParser(prog="agent-router", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    route = sub.add_parser("route", help="grade, propose, approve, launch")
    route.add_argument("--prompt")
    route.add_argument("--prompt-file")
    route.add_argument("--b64", help="base64url JSON {prompt, backend, language, local_only, allowed_harnesses}")
    route.add_argument("--backend", choices=("laya", "jev"), default="laya")
    route.add_argument("--language", choices=("auto", "en", "fr"), default="auto")
    route.add_argument("--local-only", action="store_true")
    route.add_argument("--harness", action="append", choices=HARNESSES, help="repeatable; default: installed")
    route.add_argument("--routes", default=os.environ.get("AGENT_ROUTER_ROUTES"))
    route.add_argument("--worktree", default="active", help="ORCA worktree selector")
    route.add_argument("--yes", action="store_true", help="launch the recommended route without asking")
    route.add_argument("--propose-only", action="store_true")
    route.add_argument("--dry-run", action="store_true", help="show the launch plan, launch nothing")
    route.add_argument("--json", action="store_true")
    route.set_defaults(func=cmd_route)
    health = sub.add_parser("health", help="classifier reachability, no prompt sent")
    health.add_argument("--backend", choices=("laya", "jev"))
    health.set_defaults(func=cmd_health)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except ProviderError as exc:
        print(f"classifier error: {exc}\nNo agent was launched; choose a route manually in ORCA.", file=sys.stderr)
        return 3
    except LaunchError as exc:
        print(f"launch error: {exc}", file=sys.stderr)
        return 4
    except KeyboardInterrupt:
        print("\nNothing launched.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
