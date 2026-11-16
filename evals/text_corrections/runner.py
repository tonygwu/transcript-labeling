"""Prepare isolated requests, then score retained model envelopes offline.

No provider call, retries, repair, truncation, or access to live datasets.
"""
import argparse
import json
from pathlib import Path

from transcript_labeling.contextual import build_request, resolve_response
from transcript_labeling.contextual_cli import _read, _write
from transcript_labeling.items import make_item

from .cases import CASES


def requests():
    return {case["id"]: build_request(make_item("calls/example", case["text"]), case["speakers"], unclear="unclear")
            for case in CASES}


def evaluate(envelopes, model):
    from .scoring import score
    prepared = requests()
    if set(envelopes) != set(prepared):
        raise ValueError("provide exactly one response envelope for every eval case")
    results, errors = {}, {}
    for case_id, request in prepared.items():
        try:
            bundle = resolve_response(request, envelopes[case_id], requested_model=model, run_id=f"eval-{case_id}")
            failures = score(case_id, bundle)
        except ValueError as exc:
            failures = ["invalid_response"]
            results[case_id] = {"error": str(exc)}
        else:
            results[case_id] = {"model_identity": bundle["provenance"]["model_identity"],
                                "served_model": bundle["provenance"]["served_model"]}
        results[case_id]["errors"] = failures
        for category in failures:
            errors[category] = errors.get(category, 0) + 1
    succeeded = sum(not r["errors"] for r in results.values())
    return {"attempted": len(results), "succeeded": succeeded, "failed": len(results) - succeeded,
            "errors": errors, "cases": results}


def main():
    parser = argparse.ArgumentParser(__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--out-dir", required=True, type=Path)
    scoring = sub.add_parser("score")
    scoring.add_argument("--responses", required=True, type=Path, help="directory containing CASE_ID.json envelopes")
    scoring.add_argument("--model", required=True)
    args = parser.parse_args()
    if args.action == "prepare":
        for case_id, request in requests().items():
            _write(args.out_dir / f"{case_id}.json", request)
        print(json.dumps({"prepared": len(CASES), "model_executed": False}))
        return 0
    result = evaluate({c["id"]: _read(args.responses / f"{c['id']}.json") for c in CASES}, args.model)
    print(json.dumps(result, indent=2))
    return int(result["failed"] > 0)


if __name__ == "__main__":
    raise SystemExit(main())
