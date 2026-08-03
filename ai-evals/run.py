from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from dotenv import load_dotenv

from evaluator import (JudgeConfiguration, ModelConfiguration, run_evaluation,
                       summarize)
from careerdesk.platform.ai.client import build_llm, close_llm_client
from careerdesk.platform.ai.providers import (
    provider_model_capabilities,
    provider_spec,
)


ROOT = Path(__file__).resolve().parent
REPOSITORY_ROOT = ROOT.parent
DEFAULT_CONFIG = ROOT / "config.json"
DEFAULT_DATA_DIR = ROOT / "data"
VALID_SUITES = (
    "routing",
    "agent",
    "extraction",
    "grounding",
    "plan",
    "position",
    "grill",
    "resume",
    "adaptation",
    "questions",
    "quality",
)
VALID_CONFIG_KEYS = {
    "acknowledge_model_costs",
    "case_timeout_seconds",
    "context_window",
    "enforce_targets",
    "judge_context_window",
    "judge_max_output_tokens",
    "judge_model",
    "judge_pricing",
    "judge_samples",
    "max_output_tokens",
    "max_total_tokens",
    "model",
    "pairwise_baseline",
    "pricing",
    "release_adaptation",
    "repetitions",
    "smoke",
    "suites",
    "targets",
}
VALID_TARGETS = {
    "agent_task_completion",
    "adaptation_accuracy",
    "extraction_field_accuracy",
    "grill_accuracy",
    "grounding_accuracy",
    "plan_accuracy",
    "position_accuracy",
    "quality_score",
    "questions_accuracy",
    "resume_accuracy",
    "routing_accuracy",
    "tool_argument_accuracy",
}
VALID_PRICING_KEYS = {"input_per_million", "output_per_million", "currency"}
MIN_TOKEN_BUDGET = 10_000
RELEASE_ADAPTATION_MIN_ACCURACY = 0.90
RELEASE_ADAPTATION_MIN_REPETITIONS = 3
ADAPTATION_IMPLEMENTATION_PATHS = (
    "backend/src/careerdesk/features/applications/public.py",
    "backend/src/careerdesk/features/applications/repository/prep.py",
    "backend/src/careerdesk/features/applications/repository/resume_binding.py",
    "backend/src/careerdesk/features/research/contracts.py",
    "backend/src/careerdesk/features/research/public.py",
    "backend/src/careerdesk/orchestration/application_prep/adaptation.py",
    "backend/src/careerdesk/orchestration/application_prep/adaptation_contracts.py",
    "backend/src/careerdesk/orchestration/application_prep/adaptation_workflow.py",
    "backend/src/careerdesk/orchestration/application_prep/ai_tasks.py",
    "backend/src/careerdesk/orchestration/application_prep/api.py",
    "backend/src/careerdesk/orchestration/application_prep/factory.py",
    "backend/src/careerdesk/orchestration/application_prep/http_contracts.py",
    "backend/src/careerdesk/platform/ai/client.py",
    "backend/src/careerdesk/platform/ai/providers.py",
    "backend/src/careerdesk/platform/ai/structured_tasks.py",
    "ai-evals/evaluator.py",
    "ai-evals/cases/adaptation.json",
    "ai-evals/run.py",
)
SENSITIVE_KEY = re.compile(
    r"(?:^|_)(?:api_?key|access_token|auth_token|secret|password|credential)(?:$|_)",
    re.I,
)


def _optional_int(value: str) -> int | None:
    return int(value) if value.strip() else None


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行 CareerDesk 真实模型 AI 指标评测")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--model")
    parser.add_argument("--context-window", type=_optional_int)
    parser.add_argument("--max-output-tokens", type=_optional_int)
    parser.add_argument("--suite", action="append", choices=VALID_SUITES)
    parser.add_argument("--repetitions", type=int)
    parser.add_argument("--smoke", action="store_true",
                        help="每个所选套件只跑第一条用例，用于低成本验证配置与链路")
    parser.add_argument(
        "--release-adaptation",
        action="store_true",
        help=(
            "运行简历适配正式发布门：仅 adaptation，至少重复 3 次，"
            "强制 adaptation_accuracy >= 0.90 且未达标返回失败"
        ),
    )
    parser.add_argument("--judge-model",
                        help="质量层裁判模型（provider[:model]，须不同于被测模型）")
    parser.add_argument("--pairwise-baseline",
                        help="data/ 下的基线运行目录名；质量层对其做成对比较")
    parser.add_argument("--agreement", type=Path,
                        help="只计算已填写 human_passed 的校准表与裁判的一致率，不发起任何模型调用")
    parser.add_argument("--acknowledge-costs", action="store_true")
    return parser.parse_args()


def _read_configuration(arguments: argparse.Namespace) -> dict:
    if arguments.config.is_file():
        config = json.loads(arguments.config.read_text(encoding="utf-8"))
    elif arguments.model:
        config = {}
    else:
        raise ValueError(
            f"缺少 {arguments.config}；请复制 config.example.json 为 config.json 后填写模型"
        )
    if not isinstance(config, dict):
        raise ValueError("评测配置必须是 JSON object")
    def contains_sensitive_key(value) -> bool:
        if isinstance(value, dict):
            return any(
                SENSITIVE_KEY.search(str(key)) or contains_sensitive_key(item)
                for key, item in value.items()
            )
        if isinstance(value, list):
            return any(contains_sensitive_key(item) for item in value)
        return False

    if contains_sensitive_key(config):
        raise ValueError("config.json 不能保存 API key、token、secret、password 或 credential")
    unknown = set(config) - VALID_CONFIG_KEYS
    if unknown:
        raise ValueError(f"config.json 包含未知字段：{', '.join(sorted(unknown))}")

    if arguments.model:
        config["model"] = arguments.model
    if arguments.context_window is not None:
        config["context_window"] = arguments.context_window
    if arguments.max_output_tokens is not None:
        config["max_output_tokens"] = arguments.max_output_tokens
    if arguments.suite:
        config["suites"] = arguments.suite
    if arguments.repetitions is not None:
        config["repetitions"] = arguments.repetitions
    if arguments.smoke:
        config["smoke"] = True
    if arguments.judge_model:
        config["judge_model"] = arguments.judge_model
    if arguments.pairwise_baseline:
        config["pairwise_baseline"] = arguments.pairwise_baseline
    if arguments.acknowledge_costs:
        config["acknowledge_model_costs"] = True
    if arguments.release_adaptation:
        config["release_adaptation"] = True
    return config


def _apply_release_adaptation_policy(config: dict) -> None:
    """把正式适配发布运行收敛为不可降级的单套件硬门。"""
    release_mode = config.get("release_adaptation", False)
    if type(release_mode) is not bool:
        raise ValueError("release_adaptation 必须是 boolean")
    if not release_mode:
        return
    if config.get("smoke") is True:
        raise ValueError("--release-adaptation 不能与 smoke 模式同时使用")

    repetitions = config.get("repetitions", 1)
    if type(repetitions) is int:
        config["repetitions"] = max(RELEASE_ADAPTATION_MIN_REPETITIONS, repetitions)
    config["suites"] = ["adaptation"]
    config["enforce_targets"] = True

    targets = config.get("targets", {})
    if isinstance(targets, dict):
        current_target = targets.get("adaptation_accuracy")
        if current_target is None:
            release_target = RELEASE_ADAPTATION_MIN_ACCURACY
        elif type(current_target) in (int, float):
            release_target = max(
                RELEASE_ADAPTATION_MIN_ACCURACY,
                current_target,
            )
        else:
            release_target = current_target
        config["targets"] = {"adaptation_accuracy": release_target}


def _validate_pricing(pricing) -> None:
    if pricing is None:
        return
    if not isinstance(pricing, dict):
        raise ValueError("pricing 必须是 object 或 null")
    unknown = set(pricing) - VALID_PRICING_KEYS
    if unknown:
        raise ValueError(f"pricing 包含未知字段：{', '.join(sorted(unknown))}")
    for name in ("input_per_million", "output_per_million"):
        value = pricing.get(name)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
            raise ValueError(f"pricing.{name} 必须是不小于 0 的数字")
    currency = pricing.get("currency", "USD")
    if not isinstance(currency, str) or not 1 <= len(currency.strip()) <= 8:
        raise ValueError("pricing.currency 必须是 1 到 8 个字符的文本")


def _validate_model_and_capacity(config: dict, *, model_key: str, window_key: str,
                                 output_key: str, label: str) -> None:
    model = config.get(model_key)
    if not isinstance(model, str) or not model.strip() or model == "provider:model-name":
        raise ValueError(f"请填写 provider[:model] 格式的{label}")
    config[model_key] = model.strip()
    provider = model.partition(":")[0].strip()
    if provider_spec(provider) is None:
        raise ValueError(f"未知{label} provider：{provider}")
    context_window = config.get(window_key)
    max_output_tokens = config.get(output_key)
    if (context_window is None) != (max_output_tokens is None):
        raise ValueError(f"{window_key} 与 {output_key} 必须同时填写或同时为 null")
    if context_window is None:
        provider_context, provider_output = provider_model_capabilities(config[model_key])
        if provider_context is None or provider_output is None:
            raise ValueError(f"当前{label}的具体型号必须填写 {window_key} 与 {output_key}")
    elif (
        type(context_window) is not int
        or type(max_output_tokens) is not int
        or context_window < 1_024
        or max_output_tokens < 256
        or max_output_tokens > context_window
    ):
        raise ValueError(
            f"{label}容量必须满足 context_window >= 1024 且 "
            "256 <= max_output_tokens <= context_window"
        )


def _validate_configuration(config: dict) -> None:
    _apply_release_adaptation_policy(config)
    _validate_model_and_capacity(
        config, model_key="model", window_key="context_window",
        output_key="max_output_tokens", label="被测模型",
    )
    if config.get("acknowledge_model_costs") is not True:
        raise ValueError("确认可能产生模型费用后，将 acknowledge_model_costs 改为 true")
    suites = config.get("suites", list(VALID_SUITES))
    if (
        not isinstance(suites, list)
        or not suites
        or len(suites) != len(set(suites))
        or any(type(item) is not str or item not in VALID_SUITES for item in suites)
    ):
        raise ValueError(f"suites 只能从 {', '.join(VALID_SUITES)} 中选择")
    if "suites" not in config and not config.get("judge_model"):
        # 默认全量但没配裁判时自动跳过质量层；显式点名 quality 则必须配裁判。
        suites = [suite for suite in suites if suite != "quality"]
    config["suites"] = suites
    if "quality" in suites:
        if not config.get("judge_model"):
            raise ValueError("quality 套件需要配置 judge_model 作为独立裁判模型")
        _validate_model_and_capacity(
            config, model_key="judge_model", window_key="judge_context_window",
            output_key="judge_max_output_tokens", label="裁判模型",
        )
        if config["judge_model"] == config["model"]:
            raise ValueError("裁判模型不能与被测模型相同（自偏好会污染质量分）")
        _validate_pricing(config.get("judge_pricing"))
    judge_samples = config.get("judge_samples", 3)
    if type(judge_samples) is not int or not 1 <= judge_samples <= 5:
        raise ValueError("judge_samples 必须是 1 到 5 的整数")
    pairwise_baseline = config.get("pairwise_baseline")
    if pairwise_baseline is not None:
        if not isinstance(pairwise_baseline, str) or not pairwise_baseline.strip():
            raise ValueError("pairwise_baseline 必须是 data/ 下的运行目录名")
        if "quality" not in suites:
            raise ValueError("pairwise_baseline 需要同时运行 quality 套件")
    timeout = config.get("case_timeout_seconds", 90)
    if type(timeout) is not int or not 10 <= timeout <= 300:
        raise ValueError("case_timeout_seconds 必须是 10 到 300 的整数")
    repetitions = config.get("repetitions", 1)
    if type(repetitions) is not int or not 1 <= repetitions <= 5:
        raise ValueError("repetitions 必须是 1 到 5 的整数")
    for name in ("enforce_targets", "release_adaptation", "smoke"):
        if name in config and type(config[name]) is not bool:
            raise ValueError(f"{name} 必须是 boolean")
    max_total_tokens = config.get("max_total_tokens")
    if max_total_tokens is not None and (
        type(max_total_tokens) is not int or max_total_tokens < MIN_TOKEN_BUDGET
    ):
        raise ValueError(f"max_total_tokens 必须是不小于 {MIN_TOKEN_BUDGET} 的整数或 null")
    _validate_pricing(config.get("pricing"))
    targets = config.get("targets", {})
    if not isinstance(targets, dict) or any(
        name not in VALID_TARGETS
        or type(value) not in (int, float)
        or not 0 <= value <= 1
        for name, value in targets.items()
    ):
        raise ValueError("targets 必须是名称到 0..1 数值的 object")


def _configure_environment(config: dict) -> None:
    os.environ["APP_RUNTIME_MODE"] = "test"
    provider = config["model"].partition(":")[0].strip()
    spec = provider_spec(provider)
    eval_key = os.environ.get("CAREERDESK_LLM_EVAL_API_KEY", "").strip()
    if eval_key and spec and spec.key_envs:
        os.environ[spec.key_envs[0]] = eval_key
    base_url = os.environ.get("CAREERDESK_LLM_EVAL_BASE_URL", "").strip()
    if base_url and provider == "openai_compatible":
        os.environ["OPENAI_BASE_URL"] = base_url


async def _preflight_model(config: dict) -> None:
    llm = build_llm(
        config["model"],
        strict_offline=False,
        context_window=config.get("context_window"),
        max_output_tokens=config.get("max_output_tokens"),
    )
    await close_llm_client(llm)
    if "quality" in config.get("suites", ()):
        judge = build_llm(
            config["judge_model"],
            strict_offline=False,
            context_window=config.get("judge_context_window"),
            max_output_tokens=config.get("judge_max_output_tokens"),
        )
        await close_llm_client(judge)


def _load_pairwise_baseline(config: dict, dataset_fingerprint: str) -> dict[str, dict]:
    """加载基线运行的质量输出；数据集指纹不一致直接拒绝，保证可比性。"""
    baseline_dir = DEFAULT_DATA_DIR / config["pairwise_baseline"]
    baseline_metadata = json.loads(
        (baseline_dir / "run.json").read_text(encoding="utf-8"),
    )
    if baseline_metadata.get("dataset_fingerprint") != dataset_fingerprint:
        raise ValueError(
            "基线运行的 dataset_fingerprint 与当前不一致，成对比较不可比；"
            "请在同一数据集版本下重跑基线"
        )
    baseline_results = json.loads(
        (baseline_dir / "results.json").read_text(encoding="utf-8"),
    )
    return {
        result["case_id"]: result["candidate_output"]
        for result in baseline_results
        if result.get("suite") == "quality"
        and isinstance(result.get("candidate_output"), dict)
    }


def _agreement_report(path: Path) -> int:
    """离线计算校准表与裁判的一致率；不发起任何模型调用。"""
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"校准表无法读取：{error}", file=sys.stderr)
        return 2
    labeled = [
        row for row in rows
        if isinstance(row, dict) and isinstance(row.get("human_passed"), bool)
    ]
    if not labeled:
        print("校准表里没有已填写 human_passed 的行；先人工填写再计算", file=sys.stderr)
        return 2
    agreements = [
        row["human_passed"] == bool(row.get("judge_passed")) for row in labeled
    ]
    print(f"已标注 {len(labeled)} 行；裁判与人工一致率 "
          f"{sum(agreements) / len(labeled):.1%}")
    by_criterion: dict[str, list[bool]] = {}
    for row, agreed in zip(labeled, agreements):
        by_criterion.setdefault(str(row.get("criterion_id")), []).append(agreed)
    for criterion_id, values in sorted(by_criterion.items()):
        print(f"- {criterion_id}: {sum(values) / len(values):.1%} ({sum(values)}/{len(values)})")
    print("一致率明显低于 85% 时，先修 rubric 或换裁判，再使用 quality_score。")
    return 0


def _quality_review_sheet(results: list[dict], criteria_texts: dict) -> list[dict]:
    rows = []
    for result in results:
        if result.get("suite") != "quality" or result.get("error_type"):
            continue
        for item in result["assertions"]:
            criterion_id = item["name"].removeprefix("criterion:")
            actual = item.get("actual") if isinstance(item.get("actual"), dict) else {}
            rows.append({
                "case_id": result["case_id"],
                "attempt": result.get("attempt", 1),
                "criterion_id": criterion_id,
                "criterion_text": criteria_texts.get(
                    (result["case_id"], criterion_id), "",
                ),
                "judge_passed": item["passed"],
                "evidence": actual.get("evidence"),
                "output_excerpt": json.dumps(
                    result.get("candidate_output"), ensure_ascii=False,
                )[:600],
                "human_passed": None,
            })
    return rows


def _git_commit() -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPOSITORY_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _git_paths_dirty(paths: tuple[str, ...]) -> bool | None:
    result = subprocess.run(
        ["git", "status", "--porcelain", "--", *paths],
        cwd=REPOSITORY_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    return bool(result.stdout.strip()) if result.returncode == 0 else None


def _implementation_manifest() -> tuple[dict[str, dict], str]:
    """记录适配产线与评测入口的实际字节，不依赖 worktree 已提交。"""
    manifest: dict[str, dict] = {}
    fingerprint_parts = []
    for relative_path in ADAPTATION_IMPLEMENTATION_PATHS:
        payload = (REPOSITORY_ROOT / relative_path).read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        manifest[relative_path] = {
            "sha256": digest,
            "size_bytes": len(payload),
        }
        fingerprint_parts.append(f"{relative_path}:{digest}")
    fingerprint = hashlib.sha256(
        "\n".join(fingerprint_parts).encode("utf-8"),
    ).hexdigest()
    return manifest, fingerprint


def _implementation_provenance(config: dict) -> dict:
    manifest, fingerprint = _implementation_manifest()
    return {
        "release_adaptation": config.get("release_adaptation") is True,
        "implementation_files": manifest,
        "implementation_fingerprint": fingerprint,
        "implementation_worktree_dirty": _git_paths_dirty(
            ADAPTATION_IMPLEMENTATION_PATHS,
        ),
    }


def _dataset_manifest(config: dict, *, smoke: bool = False) -> tuple[dict, str, int]:
    names = list(config.get("suites", VALID_SUITES))
    manifest = {}
    fingerprint_parts = []
    planned = 0
    repetitions = 1 if smoke else config.get("repetitions", 1)
    for name in names:
        path = ROOT / "cases" / f"{name}.json"
        payload = path.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        cases = json.loads(payload)
        case_count = len(cases)
        executed_cases = min(case_count, 1) if smoke else case_count
        manifest[name] = {
            "path": f"cases/{name}.json",
            "sha256": digest,
            "case_count": case_count,
        }
        fingerprint_parts.append(f"{name}:{digest}")
        planned += executed_cases * repetitions
    fingerprint = hashlib.sha256("\n".join(fingerprint_parts).encode()).hexdigest()
    return manifest, fingerprint, planned


def _write_json(path: Path, value) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


def _format_tokens(value: int) -> str:
    return f"{value:,}"


def _usage_lines(summary: dict) -> list[str]:
    usage = summary.get("usage")
    if not usage:
        return ["", "本次运行没有捕获到任何 token 用量（供应商未返回 usage 或没有模型用例执行）。"]
    lines = [
        "",
        "## Token 用量与成本",
        "",
        "| 套件 | LLM 调用 | 输入 tokens | 输出 tokens | 合计 tokens |",
        "|---|---:|---:|---:|---:|",
    ]
    for suite, suite_usage in usage["by_suite"].items():
        lines.append(
            f"| `{suite}` | {suite_usage['llm_calls']} "
            f"| {_format_tokens(suite_usage['input_tokens'])} "
            f"| {_format_tokens(suite_usage['output_tokens'])} "
            f"| {_format_tokens(suite_usage['total_tokens'])} |"
        )
    total = usage["total"]
    lines.append(
        f"| **合计** | {total['llm_calls']} "
        f"| {_format_tokens(total['input_tokens'])} "
        f"| {_format_tokens(total['output_tokens'])} "
        f"| {_format_tokens(total['total_tokens'])} |"
    )
    if total["usage_missing_calls"]:
        lines.append("")
        lines.append(
            f"- 有 {total['usage_missing_calls']} 次调用供应商未返回 usage，"
            "合计数字按 0 计入，实际消耗略高。"
        )
    judge_total = usage.get("judge_total")
    if judge_total:
        lines.append("")
        lines.append(
            f"- 质量层裁判用量：{_format_tokens(judge_total['total_tokens'])} tokens"
            f"（输入 {_format_tokens(judge_total['input_tokens'])} / "
            f"输出 {_format_tokens(judge_total['output_tokens'])}，"
            f"调用 {judge_total['llm_calls']} 次；不计入上表被测模型对比口径）。"
        )
    cost = usage.get("estimated_cost")
    if cost:
        lines.append("")
        lines.append(
            f"- 被测模型估算费用：{cost['total']} {cost['currency']}"
            f"（输入 {cost['input']} + 输出 {cost['output']}；按 config 单价，仅供预算参考）。"
        )
    judge_cost = usage.get("judge_estimated_cost")
    if judge_cost:
        lines.append(
            f"- 裁判估算费用：{judge_cost['total']} {judge_cost['currency']}"
            f"（输入 {judge_cost['input']} + 输出 {judge_cost['output']}）。"
        )
    return lines


def _report(summary: dict, metadata: dict) -> str:
    lines = [
        "# CareerDesk AI 指标报告",
        "",
        f"- 模型：`{metadata['model']}`",
        f"- 时间：`{metadata['started_at']}`",
        f"- Git：`{metadata.get('git_commit') or 'unknown'}`",
        f"- 数据集指纹：`{metadata['dataset_fingerprint']}`",
        f"- 适配实现指纹：`{metadata['implementation_fingerprint']}`",
        f"- 重复次数：{metadata['repetitions']}",
        f"- 用例执行：{summary['case_count']}（质量失败 {summary['failed_case_count']}）",
        f"- 执行错误：{summary['error_count']}；安全违规：{summary['safety_violation_count']}",
        f"- 重复运行不稳定用例：{summary['unstable_case_count']}；"
        f"全部重复均通过的用例占比：{summary['stable_case_pass_rate']:.1%}",
        f"- 延迟：P50 {summary['latency_ms']['p50']:.0f} ms；P95 {summary['latency_ms']['p95']:.0f} ms",
    ]
    if metadata.get("smoke"):
        lines.insert(2, "- **冒烟模式**：每套件只跑第一条用例，结果仅用于验证链路，不可作为选型基线。")
    if metadata.get("release_adaptation"):
        lines.insert(
            2,
            "- **简历适配正式发布门**：仅 adaptation，至少 3 次重复，"
            "adaptation_accuracy 目标不低于 90%。",
        )
    budget = summary.get("budget")
    if budget and budget["exhausted"]:
        lines.append(
            f"- **Token 预算耗尽**：已花 {_format_tokens(budget['spent_total_tokens'])}"
            f"（上限 {_format_tokens(budget['max_total_tokens'])}），"
            f"跳过 {budget['skipped_case_executions']} 次用例执行，结果不完整。"
        )
    lines.extend([
        "",
        "| 指标 | 结果 | 目标 | 达标 |",
        "|---|---:|---:|---:|",
    ])
    for name, metric in summary["metrics"].items():
        target = "—" if metric["target"] is None else f"{metric['target']:.0%}"
        target_status = "—" if metric["target"] is None else (
            "是" if metric["target_met"] else "否"
        )
        lines.append(
            f"| `{name}` | {metric['value']:.1%} ({metric['passed']}/{metric['total']}) "
            f"| {target} | {target_status} |"
        )
    if metadata.get("judge_model"):
        lines.extend([
            "",
            f"质量层裁判：`{metadata['judge_model']}`，每例采样 {metadata['judge_samples']} 次"
            "多数表决；quality 结果只与同裁判、同采样数、同数据集指纹的运行可比。",
        ])
    pairwise = summary.get("pairwise")
    if pairwise:
        lines.extend([
            "",
            f"成对比较（对基线 `{pairwise['baseline_run']}`，交换位置两问一致才计胜负）："
            f"胜 {pairwise['wins']} / 平 {pairwise['ties']} / 负 {pairwise['losses']}，"
            f"胜率 {pairwise['win_rate']:.1%}。",
        ])
    fallback = summary.get("extraction_fallback")
    if fallback:
        lines.extend([
            "",
            f"提取兜底纠偏：{fallback['cases_touched']}/{fallback['cases_total']} 例被兜底触碰；"
            f"救回期望字段 {fallback['rescued_field_count']} 个，"
            f"写坏期望字段 {fallback['harmed_field_count']} 个"
            f"（触碰字段：{'、'.join(fallback['changed_field_names']) or '无'}）。"
            "救回数需在多个模型档位（含最弱支持档）持续为 0 才可退役词表；写坏数应恒为 0。",
        ])
    lines.extend(_usage_lines(summary))
    lines.extend([
        "",
        "这些结果只适用于本次记录的模型、配置、代码提交和数据集，不代表其他模型或未来版本。",
        "",
    ])
    return "\n".join(lines)


def _result_exit_code(summary: dict, config: dict) -> int:
    if summary["error_count"] or not summary["safety_passed"]:
        return 1
    if summary.get("budget", {}).get("exhausted"):
        return 1
    if config.get("enforce_targets") and not summary["all_targets_met"]:
        return 1
    return 0


def main() -> int:
    arguments = _arguments()
    if arguments.agreement is not None:
        return _agreement_report(arguments.agreement)
    load_dotenv(REPOSITORY_ROOT / ".env", override=False)
    try:
        config = _read_configuration(arguments)
        _validate_configuration(config)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"配置错误：{error}", file=sys.stderr)
        return 2

    _configure_environment(config)
    try:
        asyncio.run(_preflight_model(config))
    except Exception as error:
        print(
            f"模型预检失败（{type(error).__name__}）：请检查型号、容量、凭据与兼容接口地址",
            file=sys.stderr,
        )
        return 2
    smoke = bool(config.get("smoke"))
    now = datetime.now(UTC)
    model_slug = re.sub(r"[^A-Za-z0-9._-]+", "-", config["model"]).strip("-")[:80]
    run_name = f"{now.strftime('%Y%m%dT%H%M%SZ')}-{model_slug}"
    if smoke:
        run_name += "-smoke"
    if config.get("release_adaptation"):
        run_name += "-adaptation-release"
    run_dir = DEFAULT_DATA_DIR / run_name
    datasets, dataset_fingerprint, planned_case_executions = _dataset_manifest(
        config, smoke=smoke,
    )
    quality_enabled = "quality" in config["suites"]
    pairwise_baseline_outputs: dict[str, dict] = {}
    if config.get("pairwise_baseline"):
        try:
            pairwise_baseline_outputs = _load_pairwise_baseline(
                config, dataset_fingerprint,
            )
        except (OSError, ValueError, json.JSONDecodeError, KeyError) as error:
            print(f"基线加载失败：{error}", file=sys.stderr)
            return 2
    run_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    metadata = {
        "schema_version": 4,
        "model": config["model"],
        "context_window": config.get("context_window"),
        "max_output_tokens": config.get("max_output_tokens"),
        "suites": config["suites"],
        "repetitions": 1 if smoke else config.get("repetitions", 1),
        "smoke": smoke,
        "case_timeout_seconds": config.get("case_timeout_seconds", 90),
        "enforce_targets": bool(config.get("enforce_targets")),
        "targets": config.get("targets", {}),
        "pricing": config.get("pricing"),
        "max_total_tokens": config.get("max_total_tokens"),
        "judge_model": config.get("judge_model") if quality_enabled else None,
        "judge_samples": config.get("judge_samples", 3) if quality_enabled else None,
        "judge_pricing": config.get("judge_pricing") if quality_enabled else None,
        "pairwise_baseline": config.get("pairwise_baseline"),
        "datasets": datasets,
        "dataset_fingerprint": dataset_fingerprint,
        "planned_case_executions": planned_case_executions,
        "started_at": now.isoformat(),
        "git_commit": _git_commit(),
        **_implementation_provenance(config),
    }
    _write_json(run_dir / "run.json", metadata)

    model_config = ModelConfiguration(
        model=config["model"],
        context_window=config.get("context_window"),
        max_output_tokens=config.get("max_output_tokens"),
        case_timeout_seconds=config.get("case_timeout_seconds", 90),
    )
    judge_config = None
    if quality_enabled:
        judge_config = JudgeConfiguration(
            model=config["judge_model"],
            context_window=config.get("judge_context_window"),
            max_output_tokens=config.get("judge_max_output_tokens"),
            samples=config.get("judge_samples", 3),
        )
    results, run_state = asyncio.run(run_evaluation(
        ROOT,
        model_config,
        tuple(config["suites"]),
        repetitions=config.get("repetitions", 1),
        smoke=smoke,
        max_total_tokens=config.get("max_total_tokens"),
        judge=judge_config,
        pairwise_baseline=config.get("pairwise_baseline"),
        pairwise_baseline_outputs=pairwise_baseline_outputs,
    ))
    targets = config.get("targets", {})
    summary = summarize(
        results,
        targets,
        pricing=config.get("pricing"),
        judge_pricing=config.get("judge_pricing"),
        run_state=run_state,
    )
    summary["completed_at"] = datetime.now(UTC).isoformat()
    _write_json(run_dir / "results.json", results)
    _write_json(run_dir / "summary.json", summary)
    if quality_enabled:
        criteria_texts = {
            (case["id"], criterion["id"]): criterion["text"]
            for case in json.loads(
                (ROOT / "cases/quality.json").read_text(encoding="utf-8"),
            )
            for criterion in case["criteria"]
        }
        sheet = _quality_review_sheet(results, criteria_texts)
        if sheet:
            _write_json(run_dir / "quality_review_sheet.json", sheet)
    report_path = run_dir / "report.md"
    report_path.write_text(_report(summary, metadata), encoding="utf-8")
    report_path.chmod(0o600)

    print(f"评测完成：{report_path}")
    for name, metric in summary["metrics"].items():
        print(f"- {name}: {metric['value']:.1%} ({metric['passed']}/{metric['total']})")
    usage = summary.get("usage")
    if usage:
        total = usage["total"]
        cost = usage.get("estimated_cost")
        cost_text = (
            f"；估算费用 {cost['total']} {cost['currency']}" if cost else ""
        )
        print(
            f"- tokens: {_format_tokens(total['total_tokens'])}"
            f"（输入 {_format_tokens(total['input_tokens'])} / "
            f"输出 {_format_tokens(total['output_tokens'])}，"
            f"LLM 调用 {total['llm_calls']} 次）{cost_text}"
        )
        judge_total = usage.get("judge_total")
        if judge_total:
            print(
                f"- judge tokens: {_format_tokens(judge_total['total_tokens'])}"
                f"（调用 {judge_total['llm_calls']} 次）"
            )
    pairwise = summary.get("pairwise")
    if pairwise:
        print(
            f"- pairwise vs {pairwise['baseline_run']}: "
            f"胜 {pairwise['wins']} / 平 {pairwise['ties']} / 负 {pairwise['losses']}"
            f"（胜率 {pairwise['win_rate']:.1%}）"
        )
    if summary.get("budget", {}).get("exhausted"):
        print("警告：max_total_tokens 预算耗尽，剩余用例已跳过，结果不完整。", file=sys.stderr)
    return _result_exit_code(summary, config)


if __name__ == "__main__":
    raise SystemExit(main())
