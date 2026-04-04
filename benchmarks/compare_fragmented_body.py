from __future__ import annotations

import argparse
import asyncio
import json
import statistics
from pathlib import Path

from benchmarks.compare import create_worktree, percentage_improvement
from benchmarks.fragmented_body import (
    FragmentedBodyBenchmarkConfig,
    FragmentedBodyBenchmarkResult,
    run_fragmented_body_benchmark,
)
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

    config = FragmentedBodyBenchmarkConfig(
        warmup_iterations=args.warmup_iterations,
        measured_iterations=args.measured_iterations,
        chunks=args.chunks,
        chunk_size=args.chunk_size,
        delay_ms=args.delay_ms,
    )

    try:
        current_runs = [
            await run_fragmented_body_benchmark(
                PROJECT_ROOT, f"current-fragmented-run-{index + 1}", config
            )
            for index in range(args.runs)
        ]
        baseline_runs = [
            await run_fragmented_body_benchmark(
                baseline_repo, f"baseline-{args.baseline_ref}-fragmented-run-{index + 1}", config
            )
            for index in range(args.runs)
        ]
    finally:
        if cleanup is not None:
            cleanup()

    current = aggregate_results("current-fragmented", current_runs)
    baseline = aggregate_results(f"baseline-{args.baseline_ref}-fragmented", baseline_runs)
    payload = {
        "baseline_ref": args.baseline_ref,
        "runs": args.runs,
        "current": current.__dict__,
        "baseline": baseline.__dict__,
        "delta_mean_ms": current.mean_ms - baseline.mean_ms,
        "delta_median_ms": current.median_ms - baseline.median_ms,
        "delta_p95_ms": current.p95_ms - baseline.p95_ms,
        "improvement_mean_percent": percentage_improvement(current.mean_ms, baseline.mean_ms),
        "improvement_median_percent": percentage_improvement(current.median_ms, baseline.median_ms),
        "improvement_p95_percent": percentage_improvement(current.p95_ms, baseline.p95_ms),
    }
    print(json.dumps(payload, indent=2))
    if args.output_json is not None:
        Path(args.output_json).write_text(json.dumps(payload, indent=2) + "\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare local Hypercorn against upstream for fragmented HTTP/2 request bodies."
    )
    parser.add_argument("--baseline-ref", default="upstream/main")
    parser.add_argument("--baseline-path")
    parser.add_argument("--no-fetch", action="store_true")
    parser.add_argument("--warmup-iterations", type=int, default=5)
    parser.add_argument("--measured-iterations", type=int, default=50)
    parser.add_argument("--chunks", type=int, default=256)
    parser.add_argument("--chunk-size", type=int, default=128)
    parser.add_argument("--delay-ms", type=int, default=1)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--output-json")
    return parser


def aggregate_results(
    label: str, runs: list[FragmentedBodyBenchmarkResult]
) -> FragmentedBodyBenchmarkResult:
    samples_ms = [sample for run in runs for sample in run.samples_ms]
    first = runs[0]
    return FragmentedBodyBenchmarkResult(
        target_label=label,
        server_repo=first.server_repo,
        warmup_iterations=first.warmup_iterations,
        measured_iterations=first.measured_iterations * len(runs),
        chunks=first.chunks,
        chunk_size=first.chunk_size,
        delay_ms=first.delay_ms,
        samples_ms=samples_ms,
        mean_ms=statistics.fmean(samples_ms),
        median_ms=statistics.median(samples_ms),
        p95_ms=percentile(samples_ms, 0.95),
        minimum_ms=min(samples_ms),
        maximum_ms=max(samples_ms),
    )


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
