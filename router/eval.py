#!/usr/bin/env python3
"""Paired EN/FR routing evaluation, reported per backend and language.

Measures classification, not harness quality: agreement with the expected
difficulty, hard-task recall, tools detection, and latency. Nothing is launched.
"""

import argparse
import json
import os
import sys
import time

from providers import Classifier, ProviderError
from routing import LAYA_MAX_PROMPT_BYTES


def evaluate(classifier, backend, language, prompts):
    rows, failures = [], 0
    for item in prompts:
        text = item[language]
        if backend == "laya" and len(text.encode("utf-8")) > LAYA_MAX_PROMPT_BYTES:
            failures += 1
            continue
        started = time.monotonic()
        try:
            prediction = classifier.predict(backend, text, language)
        except ProviderError as exc:
            failures += 1
            rows.append({"id": item["id"], "error": str(exc)})
            continue
        signals = prediction["signals"]
        rows.append({
            "id": item["id"], "expect": item["expect"], "got": signals["difficulty"],
            "confidence": round(signals["confidence"], 2), "domain": signals["domain"],
            "needs_tools": round(signals["needs_tools"], 2), "expect_tools": item.get("tools", False),
            "latency_ms": round((time.monotonic() - started) * 1000),
        })
    scored = [r for r in rows if "got" in r]
    hard = [r for r in scored if r["expect"] == "hard"]
    tools = [r for r in scored if r["expect_tools"]]
    return {
        "backend": backend, "language": language, "n": len(prompts), "failures": failures,
        "accuracy": round(sum(r["expect"] == r["got"] for r in scored) / len(scored), 2) if scored else None,
        "hard_recall": round(sum(r["got"] == "hard" for r in hard) / len(hard), 2) if hard else None,
        "tools_recall": round(sum(r["needs_tools"] >= 0.5 for r in tools) / len(tools), 2) if tools else None,
        "median_latency_ms": sorted(r["latency_ms"] for r in scored)[len(scored) // 2] if scored else None,
        "rows": rows,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompts", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "eval_prompts.json"))
    parser.add_argument("--backend", action="append", choices=("laya", "jev"))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    with open(args.prompts, encoding="utf-8") as source:
        prompts = json.load(source)
    classifier = Classifier()
    reports = []
    for backend in args.backend or ["laya", "jev"]:
        try:
            classifier.health(backend)
        except ProviderError as exc:
            reports.append({"backend": backend, "skipped": str(exc)})
            continue
        for language in ("en", "fr"):
            reports.append(evaluate(classifier, backend, language, prompts))
    if args.json:
        print(json.dumps(reports, ensure_ascii=False, indent=2))
        return 0
    for report in reports:
        if "skipped" in report:
            print(f"{report['backend']}: skipped ({report['skipped']})")
            continue
        print(f"\n{report['backend']} / {report['language']}: accuracy {report['accuracy']}, hard recall {report['hard_recall']}, "
              f"tools recall {report['tools_recall']}, median {report['median_latency_ms']} ms, failures {report['failures']}")
        for row in report["rows"]:
            if "error" in row:
                print(f"  {row['id']:<14} ERROR {row['error']}")
            else:
                flag = " " if row["expect"] == row["got"] else "!"
                print(f" {flag}{row['id']:<14} expect {row['expect']:<9} got {row['got']:<9} conf {row['confidence']:.2f} "
                      f"tools {row['needs_tools']:.2f} {row['domain']:<15} {row['latency_ms']} ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
