from __future__ import annotations

import argparse
import asyncio
import json
import statistics
from pathlib import Path

from benchmarks.compare import create_worktree, percentage_improvement
from benchmarks.general import GeneralBenchmarkConfig, GeneralBenchmarkResult, run_general_benchmark
from benchmarks.run_load import percentile

PROJECT_ROOT = Path(__file__).resolve().parent.parent


async def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    baseline_repo: Path
    cleanup = None
    if args.baseline_path is not None:
        baseline_repo = Path(args.baseline_path).resolve()
    else:
        cleanup, baseline_repo = create_worktree(args.baseline_ref, fetch=not args.no_fetch)

    try:
        results = {}
        scenarios = [
            ("http1", "1.1", False),
            ("http1_tls", "1.1", True),
            ("http2", "2", True),
        ]
        for scenario_name, http_version, tls in scenarios:
            config = GeneralBenchmarkConfig(
                http_version=http_version,
                tls=tls,
                path=args.path,
                concurrency=args.concurrency,
                total_requests=args.total_requests,
                warmup_requests=args.warmup_requests,
            )
            current_runs = [
                await run_general_benchmark(PROJECT_ROOT, f"current-{scenario_name}-run-{index + 1}", config)
                for index in range(args.runs)
            ]
            baseline_runs = [
                await run_general_benchmark(
                    baseline_repo,
                    f"baseline-{args.baseline_ref}-{scenario_name}-run-{index + 1}",
                    config,
                )
                for index in range(args.runs)
            ]
            current = aggregate_general_results(f"current-{scenario_name}", current_runs)
            baseline = aggregate_general_results(
                f"baseline-{args.baseline_ref}-{scenario_name}", baseline_runs
            )
            results[scenario_name] = {
                "current": current.__dict__,
                "baseline": baseline.__dict__,
                "delta_mean_ms": current.mean_ms - baseline.mean_ms,
                "delta_median_ms": current.median_ms - baseline.median_ms,
                "delta_p95_ms": current.p95_ms - baseline.p95_ms,
                "delta_rps": current.requests_per_second - baseline.requests_per_second,
                "improvement_mean_percent": percentage_improvement(current.mean_ms, baseline.mean_ms),
                "improvement_median_percent": percentage_improvement(current.median_ms, baseline.median_ms),
                "improvement_p95_percent": percentage_improvement(current.p95_ms, baseline.p95_ms),
                "improvement_rps_percent": (
                    ((current.requests_per_second - baseline.requests_per_second) / baseline.requests_per_second) * 100
                    if baseline.requests_per_second > 0
                    else 0.0
                ),
            }
    finally:
        if cleanup is not None:
            cleanup()

    payload = {
        "baseline_ref": args.baseline_ref,
        "runs": args.runs,
        "concurrency": args.concurrency,
        "total_requests": args.total_requests,
        "warmup_requests": args.warmup_requests,
        "results": results,
    }
    print(json.dumps(payload, indent=2))
    if args.output_json is not None:
        Path(args.output_json).write_text(json.dumps(payload, indent=2) + "\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compare local Hypercorn against upstream in general HTTP benchmarks.")
    parser.add_argument("--baseline-ref", default="upstream/main")
    parser.add_argument("--baseline-path")
    parser.add_argument("--no-fetch", action="store_true")
    parser.add_argument("--path", default="/fast")
    parser.add_argument("--concurrency", type=int, default=50)
    parser.add_argument("--total-requests", type=int, default=500)
    parser.add_argument("--warmup-requests", type=int, default=50)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--output-json")
    return parser


def aggregate_general_results(
    label: str, runs: list[GeneralBenchmarkResult]
) -> GeneralBenchmarkResult:
    samples_ms = [sample for run in runs for sample in run.samples_ms]
    total_time_s = sum(run.total_time_s for run in runs)
    total_requests = sum(run.total_requests for run in runs)
    first = runs[0]
    return GeneralBenchmarkResult(
        target_label=label,
        server_repo=first.server_repo,
        http_version=first.http_version,
        tls=first.tls,
        path=first.path,
        concurrency=first.concurrency,
        total_requests=total_requests,
        warmup_requests=first.warmup_requests,
        total_time_s=total_time_s,
        requests_per_second=(total_requests / total_time_s) if total_time_s > 0 else 0.0,
        samples_ms=samples_ms,
        mean_ms=statistics.fmean(samples_ms),
        median_ms=statistics.median(samples_ms),
        p95_ms=percentile(samples_ms, 0.95),
        minimum_ms=min(samples_ms),
        maximum_ms=max(samples_ms),
    )


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
