from __future__ import annotations

import argparse
import asyncio
import json
import statistics
from pathlib import Path

from benchmarks.compare import create_worktree, percentage_improvement
from benchmarks.run_load import percentile
from benchmarks.ws import WebsocketBenchmarkConfig, WebsocketBenchmarkResult, run_ws_benchmark

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

    config = WebsocketBenchmarkConfig(
        tls=args.tls,
        path=args.path,
        warmup_messages=args.warmup_messages,
        measured_messages=args.measured_messages,
        payload_size=args.payload_size,
    )

    try:
        current_runs = [
            await run_ws_benchmark(PROJECT_ROOT, f"current-ws-run-{index + 1}", config)
            for index in range(args.runs)
        ]
        baseline_runs = [
            await run_ws_benchmark(
                baseline_repo,
                f"baseline-{args.baseline_ref}-ws-run-{index + 1}",
                config,
            )
            for index in range(args.runs)
        ]
    finally:
        if cleanup is not None:
            cleanup()

    current = aggregate_ws_results("current-ws", current_runs)
    baseline = aggregate_ws_results(f"baseline-{args.baseline_ref}-ws", baseline_runs)
    payload = {
        "baseline_ref": args.baseline_ref,
        "runs": args.runs,
        "current": current.__dict__,
        "baseline": baseline.__dict__,
        "delta_mean_ms": current.mean_ms - baseline.mean_ms,
        "delta_median_ms": current.median_ms - baseline.median_ms,
        "delta_p95_ms": current.p95_ms - baseline.p95_ms,
        "delta_messages_per_second": current.messages_per_second - baseline.messages_per_second,
        "improvement_mean_percent": percentage_improvement(current.mean_ms, baseline.mean_ms),
        "improvement_median_percent": percentage_improvement(current.median_ms, baseline.median_ms),
        "improvement_p95_percent": percentage_improvement(current.p95_ms, baseline.p95_ms),
        "improvement_messages_per_second_percent": (
            ((current.messages_per_second - baseline.messages_per_second) / baseline.messages_per_second) * 100
            if baseline.messages_per_second > 0
            else 0.0
        ),
    }
    print(json.dumps(payload, indent=2))
    if args.output_json is not None:
        Path(args.output_json).write_text(json.dumps(payload, indent=2) + "\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compare local Hypercorn against upstream in websocket echo benchmarks.")
    parser.add_argument("--baseline-ref", default="upstream/main")
    parser.add_argument("--baseline-path")
    parser.add_argument("--no-fetch", action="store_true")
    parser.add_argument("--tls", action="store_true")
    parser.add_argument("--path", default="/ws")
    parser.add_argument("--warmup-messages", type=int, default=50)
    parser.add_argument("--measured-messages", type=int, default=300)
    parser.add_argument("--payload-size", type=int, default=65536)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--output-json")
    return parser


def aggregate_ws_results(
    label: str, runs: list[WebsocketBenchmarkResult]
) -> WebsocketBenchmarkResult:
    samples_ms = [sample for run in runs for sample in run.samples_ms]
    total_time_s = sum(run.total_time_s for run in runs)
    total_messages = sum(run.measured_messages for run in runs)
    first = runs[0]
    return WebsocketBenchmarkResult(
        target_label=label,
        server_repo=first.server_repo,
        tls=first.tls,
        path=first.path,
        warmup_messages=first.warmup_messages,
        measured_messages=total_messages,
        payload_size=first.payload_size,
        total_time_s=total_time_s,
        messages_per_second=(total_messages / total_time_s) if total_time_s > 0 else 0.0,
        samples_ms=samples_ms,
        mean_ms=statistics.fmean(samples_ms),
        median_ms=statistics.median(samples_ms),
        p95_ms=percentile(samples_ms, 0.95),
        minimum_ms=min(samples_ms),
        maximum_ms=max(samples_ms),
    )


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
