"""Tag-only CI ordering contracts for source and packaging gates."""

from pathlib import Path
import tomllib

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
RELEASE_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "unsigned-release.yml"
LLM_EVAL_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "llm-eval.yml"
BACKEND_PYPROJECT = REPOSITORY_ROOT / "backend" / "pyproject.toml"
HATCH_BUILD = REPOSITORY_ROOT / "backend" / "hatch_build.py"
DOCKERFILE = REPOSITORY_ROOT / "Dockerfile"


def test_backend_gitleaks_scans_before_generated_artifacts():
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["quality"]["steps"]

    checkout_indexes = [
        index
        for index, step in enumerate(steps)
        if step.get("uses", "").startswith("actions/checkout@")
    ]
    assert checkout_indexes == [0]
    assert steps[checkout_indexes[0]]["with"]["persist-credentials"] is False

    scan_indexes = [
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Scan committed source for secrets"
    ]
    assert scan_indexes == [checkout_indexes[0] + 1]

    scan_command = steps[scan_indexes[0]]["run"]
    assert 'tar -xz -C "${RUNNER_TEMP}" gitleaks' in scan_command
    assert '"${RUNNER_TEMP}/gitleaks" dir . --no-banner --redact' in scan_command

    generated_artifact_indexes = [
        index
        for index, step in enumerate(steps)
        if any(
            command in step.get("run", "")
            for command in ("uv sync", "pytest", "uv build")
        )
    ]
    assert generated_artifact_indexes
    assert scan_indexes[0] < min(generated_artifact_indexes)


def test_installed_wheel_requires_and_bundles_the_prebuilt_frontend():
    configuration = tomllib.loads(BACKEND_PYPROJECT.read_text(encoding="utf-8"))
    targets = configuration["tool"]["hatch"]["build"]["targets"]
    assert targets["wheel"]["hooks"]["custom"]["path"] == "hatch_build.py"
    assert targets["sdist"]["hooks"]["custom"]["path"] == "hatch_build.py"
    hook = HATCH_BUILD.read_text(encoding="utf-8")
    assert '"src/careerdesk/frontend_dist"' in hook
    assert '"careerdesk/frontend_dist"' in hook
    assert '"src/careerdesk/default.env"' in hook
    assert '"careerdesk/default.env"' in hook
    assert hook.count("self._validate_frontend(checkout_frontend)") == 2

    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["quality"]["steps"]
    node_index = next(
        index for index, step in enumerate(steps)
        if step.get("uses", "").startswith("actions/setup-node@")
    )
    frontend_index = next(
        index for index, step in enumerate(steps)
        if step.get("name") == "Test and build the backend and frontend"
    )
    wheel_index = next(
        index for index, step in enumerate(steps)
        if step.get("name", "").startswith("Build the wheel")
    )

    assert node_index < frontend_index < wheel_index
    assert steps[node_index]["with"]["node-version"] == "${{ env.RELEASE_NODE_VERSION }}"
    assert "npm --prefix frontend ci" in steps[frontend_index]["run"]
    assert "npm --prefix frontend run build" in steps[frontend_index]["run"]


def test_ci_pins_uv_and_docker_copies_the_custom_hook_before_project_install():
    for path in (RELEASE_WORKFLOW, LLM_EVAL_WORKFLOW):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        setup_steps = [
            step
            for job in workflow["jobs"].values()
            for step in job["steps"]
            if step.get("uses", "").startswith("astral-sh/setup-uv@")
        ]
        assert setup_steps
        assert all(
            step.get("with", {}).get("version")
            in {"0.9.21", "${{ env.RELEASE_UV_VERSION }}"}
            for step in setup_steps
        )

    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    hook_copy = (
        "COPY backend/pyproject.toml backend/uv.lock backend/hatch_build.py ./backend/"
    )
    project_install = "RUN uv sync --project backend --locked --no-dev\n"
    assert dockerfile.index(hook_copy) < dockerfile.index(project_install)
