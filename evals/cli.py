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
    "load_context": "载入上下文",
    "triage": "分诊",
    "investigation_planner": "制定计划",
    "parallel_investigation": "并行取证",
    "evidence_aggregation": "汇总证据",
    "hypothesis_generation": "生成假设",
    "hypothesis_verification": "验证假设",
    "root_cause_diagnosis": "根因诊断",
    "recovery_planner": "制定方案",
    "risk_assessment": "风险评估",
    "human_approval": "人工审批",
    "recovery_executor": "执行恢复",
    "rollback": "回滚",
    "verification": "复查恢复",
    "postmortem": "生成复盘",
}


def _short(stage: str) -> str:
    return _NODE_LABELS.get(stage, stage)


def _markdown(suite: SuiteResult, report) -> str:
    """The human-readable artefact — the thing a reviewer actually reads."""
    lines: list[str] = []
    add = lines.append

    add("# OpsPilot Agent 评测报告")
    add("")
    add(f"- 运行时间：`{suite.started_at}`")
    add(f"- 场景数：**{report.total}**，取自模拟器目录")
    add(f"- 模拟器：{suite.simulator_mode}，已开启评测模式（ground truth 可达）")
    add(f"- 墙钟耗时：{suite.duration_ms / 1000:.1f}s")
    add("")
    add("## 记分卡")
    add("")
    add("| 指标 | 数值 |")
    add("| --- | --- |")
    add(f"| 根因判定准确率 | **{report.root_cause_accuracy:.1%}** |")
    add(
        f"| 证据召回率 | {report.evidence_recall:.1%} "
        f"（被诊断引用 {report.evidence_utilisation:.1%}，"
        f"可追溯到假设 {report.evidence_traceability:.1%}） |"
    )
    add(f"| 工具选择准确率 | {report.tool_selection_accuracy:.1%} |")
    add(
        f"| 调查步数 | 平均 {report.avg_investigation_steps:.1f} 步，"
        f"涉及 {report.avg_distinct_stages:.1f} 个不同阶段，"
        f"{report.runs_that_replanned}/{report.total} 次运行发生了重新规划"
        f"（平均额外跑 {report.avg_replan_rounds:.1f} 个节点），"
        f"被否假设 {report.avg_rejected_hypotheses:.1f} 个 |"
    )
    add(
        f"| 恢复成功率 | {report.recovery_success_rate:.1%} "
        f"（环境确实被修复 {report.environment_fixed_rate:.1%}） |"
    )
    add(
        f"| 验证准确率 | {report.verification_accuracy:.1%} "
        f"覆盖 {report.verification_scored} 次运行 |"
    )
    add(f"| 声称已修复但环境仍是坏的 | {report.reported_success_env_broken} |")
    add(
        f"| 误诊率 | {report.false_diagnosis_rate:.1%} "
        f"（{report.answered_runs} 次给出结论，{report.abstained_runs} 次弃权） |"
    )
    reasons = f" `{report.escalation_reasons}`" if report.escalation_reasons else ""
    add(f"| 升级率 | {report.escalation_rate:.1%}{reasons} |")
    add(
        f"| 工具调用 | 共 {report.total_tool_calls} 次，"
        f"平均 {report.avg_tool_calls:.1f}/次运行，失败 {report.failed_tool_call_rate:.1%} |"
    )
    modes = f" `{report.reasoning_modes}`" if report.reasoning_modes else ""
    add(
        f"| Token 用量 | 共 {report.total_tokens}，"
        f"平均 {report.avg_tokens:.1f}/次运行{modes} |"
    )
    add(
        f"| 延迟 | 平均 {report.avg_latency_ms:.0f}ms，"
        f"p50 {report.p50_latency_ms:.0f}ms，p95 {report.p95_latency_ms:.0f}ms，"
        f"最大 {report.max_latency_ms:.0f}ms |"
    )
    add(
        f"| 追踪完整性 | {report.traces_single_root}/{report.total} 单一根节点，"
        f"{report.traces_with_dangling_parents} 个悬空父节点，"
        f"平均 {report.avg_spans_per_run:.0f} 个 span |"
    )
    add("")

    add("## 逐场景明细")
    add("")
    add(
        "| 场景 | 期望分类 | 判定分类 | 结论 | 工具数 | 召回率 | 环境 | "
        "验证 | 延迟 |"
    )
    add("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for case in suite.cases:
        mark = "OK" if case.root_cause_correct else ("err" if case.error else "**MISS**")
        env = "不适用" if case.env_passed is None else ("已修复" if case.env_passed else "仍是坏的")
        add(
            f"| {case.scenario} | {case.expected_category} | "
            f"{case.diagnosed_category or '-'} {mark} | "
            f"{case.diagnosis_outcome or '-'} | {case.tool_call_count} | "
            f"{case.evidence_recall:.2f} | {env} | {case.agent_verdict or '-'} | "
            f"{case.latency_ms:.0f}ms |"
        )
    add("")

    add("## 实际走过的节点路径")
    add("")
    add(
        "重复本身就是结论：一次被重新规划的调查会把 `investigation_planner` → "
        "`parallel_investigation` 再跑一遍，而固定流程永远不会。"
    )
    add("")
    for case in suite.cases:
        if not case.stage_sequence:
            continue
        path = " → ".join(_short(stage) for stage in case.stage_sequence)
        n = case.replan_rounds
        add(f"- **{case.scenario}**（额外跑 {n} 个节点）：{path}")
    add("")

    add("## 提出过的假设")
    add("")
    for case in suite.cases:
        if not case.hypotheses:
            continue
        add(f"### {case.scenario}")
        add("")
        add("| 引用 | 领域 | 分类 | 置信度 | 状态 |")
        add("| --- | --- | --- | --- | --- |")
        for hyp in case.hypotheses:
            add(
                f"| {hyp.ref} | {hyp.domain} | {hyp.category} | "
                f"{hyp.confidence:.2f} | {hyp.status} |"
            )
        add("")

    if report.category_breakdown:
        add("## 按根因分类")
        add("")
        for name, value in report.category_breakdown.items():
            add(f"- {name}: {value:.1%}")
        add("")
    if report.difficulty_breakdown:
        add("## 按难度")
        add("")
        for name, value in report.difficulty_breakdown.items():
            add(f"- {name}: {value:.1%}")
        add("")

    misses = [
        c for c in suite.cases if not c.root_cause_correct and not c.error
    ]
    if misses:
        add("## 判定错误")
        add("")
        for case in misses:
            add(f"### {case.scenario}")
            add("")
            add(f"- 期望 `{case.expected_category}`（{case.expected_root_cause}）")
            add(f"- 实际判定 `{case.diagnosed_category}`（{case.diagnosis_outcome}）")
            if case.missing_evidence:
                add(f"- 始终没采集到的证据：{', '.join(case.missing_evidence)}")
            if case.missing_tools:
                add(f"- 始终没调用的工具：{', '.join(case.missing_tools)}")
            if case.escalation_reason:
                add(f"- 升级原因：{case.escalation_reason}")
            add("")

    add("## 这些数字是怎么定义的")
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
            print(f"没有匹配 {args.only!r} 的场景")
            return 2

    print("=" * 100)
    print("OpsPilot Agent 评测 —— 端到端跑完整流程，对照模拟器的答案评分")
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
    print(f"报告：{md_path}")
    print(f"JSON：{json_path}")
    if web_path is not None:
        print(f"前端：{web_path}")
    if args.keep_db:
        print(f"数据库：{suite.database}")

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
