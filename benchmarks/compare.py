from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import shutil
import subprocess
import tempfile
from pathlib import Path

from benchmarks.run_load import BenchmarkConfig, BenchmarkResult, percentile, run_benchmark

PROJECT_ROOT = Path(__file__).resolve().parent.parent


async def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    config = BenchmarkConfig(
        warmup_iterations=args.warmup_iterations,
        measured_iterations=args.measured_iterations,
        fast_streams=args.fast_streams,
        slow_chunks=args.slow_chunks,
        slow_chunk_size=args.slow_chunk_size,
        slow_delay_ms=args.slow_delay_ms,
    )

    baseline_repo: Path
    cleanup = None
    if args.baseline_path is not None:
        baseline_repo = Path(args.baseline_path).resolve()
    else:
        cleanup, baseline_repo = create_worktree(args.baseline_ref, fetch=not args.no_fetch)

    try:
        current_runs = [
            await run_benchmark(PROJECT_ROOT, f"current-run-{index + 1}", config)
            for index in range(args.runs)
        ]
        baseline_runs = [
            await run_benchmark(baseline_repo, f"baseline:{args.baseline_ref}-run-{index + 1}", config)
            for index in range(args.runs)
        ]
    finally:
        if cleanup is not None:
            cleanup()

    current = aggregate_results("current", current_runs)
    baseline = aggregate_results(f"baseline:{args.baseline_ref}", baseline_runs)
    comparison = {
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
    print(json.dumps(comparison, indent=2))
    if args.output_json is not None:
        Path(args.output_json).write_text(json.dumps(comparison, indent=2) + "\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compare local Hypercorn against upstream for the H2 HoL benchmark.")
    parser.add_argument("--baseline-ref", default="upstream/main")
    parser.add_argument("--baseline-path", help="Optional existing repo path to use as baseline instead of creating a worktree.")
    parser.add_argument("--no-fetch", action="store_true")
    parser.add_argument("--warmup-iterations", type=int, default=2)
    parser.add_argument("--measured-iterations", type=int, default=10)
    parser.add_argument("--fast-streams", type=int, default=5)
    parser.add_argument("--slow-chunks", type=int, default=12)
    parser.add_argument("--slow-chunk-size", type=int, default=4096)
    parser.add_argument("--slow-delay-ms", type=int, default=25)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--output-json")
    return parser


def create_worktree(ref: str, fetch: bool) -> tuple[callable, Path]:
    if fetch:
        subprocess.run(["git", "fetch", "upstream"], cwd=PROJECT_ROOT, check=True)

    tempdir = Path(tempfile.mkdtemp(prefix="hypercorn-bench-"))
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(tempdir), ref],
        cwd=PROJECT_ROOT,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    def cleanup() -> None:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(tempdir)],
            cwd=PROJECT_ROOT,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        shutil.rmtree(tempdir, ignore_errors=True)

    return cleanup, tempdir


def percentage_improvement(current: float, baseline: float) -> float:
    if baseline == 0:
        return 0.0
    return ((baseline - current) / baseline) * 100


def aggregate_results(label: str, runs: list[BenchmarkResult]) -> BenchmarkResult:
    samples_ms = [sample for run in runs for sample in run.samples_ms]
    first = runs[0]
    return BenchmarkResult(
        target_label=label,
        server_repo=first.server_repo,
        warmup_iterations=first.warmup_iterations,
        measured_iterations=first.measured_iterations * len(runs),
        fast_streams=first.fast_streams,
        slow_chunks=first.slow_chunks,
        slow_chunk_size=first.slow_chunk_size,
        slow_delay_ms=first.slow_delay_ms,
        samples_ms=samples_ms,
        mean_ms=statistics.fmean(samples_ms),
        median_ms=statistics.median(samples_ms),
        p95_ms=percentile(samples_ms, 0.95),
        minimum_ms=min(samples_ms),
        maximum_ms=max(samples_ms),
    )


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
