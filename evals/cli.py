"""CLI entry point for the OpsPilot evaluation.

Run from the repository root::

    python evals/cli.py                      # score every scenario
    python evals/cli.py --only redis         # one scenario
    python evals/cli.py --report-dir reports # where the artefacts go

The suite is self-contained: it starts the simulator in-process over ASGI,
migrates a scratch database, drives the real agent workflow once per scenario
(clearing the human approval gate), and writes a JSON + Markdown report.

Exit status is 0 when every scenario was either diagnosed correctly or honestly
abstained, and 1 otherwise — so this is usable as a CI gate rather than a
dashboard nobody reads.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for _relative in ("evals", "apps/backend/src", "simulator/src"):
    _path = str(_ROOT / _relative)
    if _path not in sys.path:
        sys.path.insert(0, _path)

from opspilot_evals.runners.eval_runner import (  # noqa: E402
    SuiteResult,
    _print_case,
    bootstrap_paths,
    run_suite,
)


#: Compact, unambiguous node labels for the path rendering. Explicit rather
#: than derived: ``hypothesis_verification`` and ``verification`` are different
#: nodes, and any suffix-stripping rule would collapse them into one label.
_NODE_LABELS = {
    "load_context": "ctx",
    "triage": "triage",
    "investigation_planner": "plan",
    "parallel_investigation": "collect",
    "evidence_aggregation": "assess",
    "hypothesis_generation": "hypotheses",
    "hypothesis_verification": "test",
    "root_cause_diagnosis": "diagnose",
    "recovery_planner": "recovery-plan",
    "risk_assessment": "risk",
    "human_approval": "approval",
    "recovery_executor": "execute",
    "rollback": "rollback",
    "verification": "verify",
    "postmortem": "postmortem",
}


def _short(stage: str) -> str:
    return _NODE_LABELS.get(stage, stage)


def _markdown(suite: SuiteResult, report) -> str:
    """The human-readable artefact — the thing a reviewer actually reads."""
    lines: list[str] = []
    add = lines.append

    add("# OpsPilot agent evaluation")
    add("")
    add(f"- run at: `{suite.started_at}`")
    add(f"- scenarios: **{report.total}** across the simulator catalogue")
    add(f"- simulator: {suite.simulator_mode}, eval mode on (ground truth reachable)")
    add(f"- wall clock: {suite.duration_ms / 1000:.1f}s")
    add("")
    add("## Scorecard")
    add("")
    add("| metric | value |")
    add("| --- | --- |")
    add(f"| Root cause accuracy | **{report.root_cause_accuracy:.1%}** |")
    add(
        f"| Evidence accuracy (recall) | {report.evidence_recall:.1%} "
        f"(utilised {report.evidence_utilisation:.1%}, "
        f"traceable {report.evidence_traceability:.1%}) |"
    )
    add(f"| Tool selection accuracy | {report.tool_selection_accuracy:.1%} |")
    add(
        f"| Investigation steps | avg {report.avg_investigation_steps:.1f} steps, "
        f"{report.avg_distinct_stages:.1f} distinct stages, "
        f"replanned in {report.runs_that_replanned}/{report.total} runs "
        f"({report.avg_replan_rounds:.1f} extra node runs on average), "
        f"{report.avg_rejected_hypotheses:.1f} hypotheses rejected |"
    )
    add(
        f"| Recovery success rate | {report.recovery_success_rate:.1%} "
        f"(environment actually fixed {report.environment_fixed_rate:.1%}) |"
    )
    add(
        f"| Verification accuracy | {report.verification_accuracy:.1%} "
        f"over {report.verification_scored} scored runs |"
    )
    add(f"| Reported fixed while broken | {report.reported_success_env_broken} |")
    add(
        f"| False diagnosis rate | {report.false_diagnosis_rate:.1%} "
        f"({report.answered_runs} answers, {report.abstained_runs} abstentions) |"
    )
    add(
        f"| Escalation rate | {report.escalation_rate:.1%} "
        + (f"`{report.escalation_reasons}`" if report.escalation_reasons else "")
        + " |"
    )
    add(
        f"| Tool calls | {report.total_tool_calls} total, "
        f"avg {report.avg_tool_calls:.1f}/run, {report.failed_tool_call_rate:.1%} failed |"
    )
    add(
        f"| Token usage | {report.total_tokens} total, avg {report.avg_tokens:.1f}/run "
        + (f"`{report.reasoning_modes}`" if report.reasoning_modes else "")
        + " |"
    )
    add(
        f"| Latency | avg {report.avg_latency_ms:.0f}ms, "
        f"p50 {report.p50_latency_ms:.0f}ms, p95 {report.p95_latency_ms:.0f}ms, "
        f"max {report.max_latency_ms:.0f}ms |"
    )
    add(
        f"| Trace integrity | {report.traces_single_root}/{report.total} single-root, "
        f"{report.traces_with_dangling_parents} dangling, avg {report.avg_spans_per_run:.0f} spans |"
    )
    add("")

    add("## Per scenario")
    add("")
    add(
        "| scenario | expected | diagnosed | outcome | tools | recall | env | "
        "verify | latency |"
    )
    add("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for case in suite.cases:
        mark = "OK" if case.root_cause_correct else ("err" if case.error else "**MISS**")
        env = "n/a" if case.env_passed is None else ("fixed" if case.env_passed else "broken")
        add(
            f"| {case.scenario} | {case.expected_category} | "
            f"{case.diagnosed_category or '-'} {mark} | "
            f"{case.diagnosis_outcome or '-'} | {case.tool_call_count} | "
            f"{case.evidence_recall:.2f} | {env} | {case.agent_verdict or '-'} | "
            f"{case.latency_ms:.0f}ms |"
        )
    add("")

    add("## Node path actually taken")
    add("")
    add(
        "Repeats are the point: a re-planned investigation runs "
        "`investigation_planner` → `parallel_investigation` again, which a fixed "
        "pipeline never does."
    )
    add("")
    for case in suite.cases:
        if not case.stage_sequence:
            continue
        path = " → ".join(_short(stage) for stage in case.stage_sequence)
        n = case.replan_rounds
        add(
            f"- **{case.scenario}** "
            f"({n} extra node {'run' if n == 1 else 'runs'}): {path}"
        )
    add("")

    add("## Hypotheses raised")
    add("")
    for case in suite.cases:
        if not case.hypotheses:
            continue
        add(f"### {case.scenario}")
        add("")
        add("| ref | domain | category | confidence | status |")
        add("| --- | --- | --- | --- | --- |")
        for hyp in case.hypotheses:
            add(
                f"| {hyp.ref} | {hyp.domain} | {hyp.category} | "
                f"{hyp.confidence:.2f} | {hyp.status} |"
            )
        add("")

    if report.category_breakdown:
        add("## By root-cause category")
        add("")
        for name, value in report.category_breakdown.items():
            add(f"- {name}: {value:.1%}")
        add("")
    if report.difficulty_breakdown:
        add("## By difficulty")
        add("")
        for name, value in report.difficulty_breakdown.items():
            add(f"- {name}: {value:.1%}")
        add("")

    misses = [
        c for c in suite.cases if not c.root_cause_correct and not c.error
    ]
    if misses:
        add("## Misses")
        add("")
        for case in misses:
            add(f"### {case.scenario}")
            add("")
            add(f"- expected `{case.expected_category}` ({case.expected_root_cause})")
            add(f"- diagnosed `{case.diagnosed_category}` ({case.diagnosis_outcome})")
            if case.missing_evidence:
                add(f"- evidence never seen: {', '.join(case.missing_evidence)}")
            if case.missing_tools:
                add(f"- tools never called: {', '.join(case.missing_tools)}")
            if case.escalation_reason:
                add(f"- escalated: {case.escalation_reason}")
            add("")

    add("## How the numbers are defined")
    add("")
    for key, value in report.notes.get("scoring", {}).items():
        add(f"- **{key}** — {value}")
    add("")
    return "\n".join(lines)


async def _run(args: argparse.Namespace) -> int:
    bootstrap_paths()
    scenarios = None
    if args.only:
        from opspilot_evals.datasets.ground_truth import load_eval_cases

        scenarios = [c.scenario for c in load_eval_cases() if args.only in c.scenario]
        if not scenarios:
            print(f"no scenario matches {args.only!r}")
            return 2

    print("=" * 100)
    print("OpsPilot agent evaluation — scored end to end against the simulator's answer key")
    print("=" * 100)

    suite = await run_suite(
        scenarios=scenarios,
        db_path=args.db or None,
        keep_db=args.keep_db,
    )
    for case in suite.cases:
        _print_case(case)

    from opspilot_evals.metrics.aggregator import build_report

    report = build_report(suite)
    print()
    print("-" * 100)
    for line in report.headline():
        print(line)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": stamp,
        "suite": suite.as_dict(),
        "report": report.as_dict(),
    }
    json_path = report_dir / f"eval-{stamp}.json"
    md_path = report_dir / f"eval-{stamp}.md"
    json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    md_path.write_text(_markdown(suite, report), encoding="utf-8")
    # A stable "latest" pair, so a README badge or a CI job has a fixed path to
    # read instead of globbing for the newest timestamp.
    (report_dir / "latest.json").write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8"
    )
    (report_dir / "latest.md").write_text(_markdown(suite, report), encoding="utf-8")

    # The frontend's offline-benchmark screen renders this report. Emitting it
    # here makes the page a view of a generated artefact rather than a
    # hand-copied snapshot that drifts the moment the suite changes.
    #
    # Only the aggregate `report` block and the timestamp are exported: the
    # page has no use for `suite.cases` (megabytes) or for the run environment
    # (scratch database paths and flags that describe the machine that ran the
    # suite, not the suite itself).
    web_path = _web_report_path()
    if web_path is not None:
        web_path.write_text(
            json.dumps(
                {"generated_at": stamp, "report": report.as_dict()},
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )

    print()
    print(f"report: {md_path}")
    print(f"json:   {json_path}")
    if web_path is not None:
        print(f"web:    {web_path}")
    if args.keep_db:
        print(f"db:     {suite.database}")

    return 0 if report.failures() == 0 else 1


def _web_report_path() -> Path | None:
    """Where the frontend expects its copy of the report, if the repo is here.

    Returns `None` when `evals/` has been checked out on its own, so the suite
    still runs standalone instead of failing on a missing sibling directory.
    """
    root = Path(__file__).resolve().parent.parent
    lib = root / "apps" / "frontend" / "src" / "lib"
    if not lib.is_dir():
        return None
    return lib / "eval_report.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", default="", help="substring filter on scenario names")
    parser.add_argument("--db", default="", help="reuse this database file")
    parser.add_argument("--keep-db", action="store_true", help="keep the scratch database")
    parser.add_argument(
        "--report-dir",
        default=str(Path(__file__).resolve().parent / "reports"),
        help="where the JSON + Markdown reports are written",
    )
    args = parser.parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
