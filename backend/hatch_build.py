"""Package the already-built frontend without making wheel installs require Node."""

from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    """Use checkout dist directly, or the copy embedded in an sdist."""

    def initialize(self, version: str, build_data: dict) -> None:
        # `uv sync` asks Hatch for an editable wheel. Source checkouts resolve
        # frontend/default resources from the repository, so forcing release
        # resources here creates a build-before-sync cycle on clean machines.
        if version == "editable":
            return
        project_root = Path(self.root)
        checkout_frontend = project_root.parent / "frontend" / "dist"
        checkout_default_env = project_root.parent / ".env.example"
        sdist_frontend = project_root / "src" / "careerdesk" / "frontend_dist"
        sdist_default_env = project_root / "src" / "careerdesk" / "default.env"

        if self.target_name == "sdist":
            self._validate_frontend(checkout_frontend)
            self._validate_default_env(checkout_default_env)
            build_data["force_include"].update(
                {
                    str(checkout_frontend): "src/careerdesk/frontend_dist",
                    str(checkout_default_env): "src/careerdesk/default.env",
                }
            )
        elif self.target_name == "wheel":
            # When Hatch builds a wheel from our sdist these resources already
            # live below src/careerdesk, so the normal package traversal owns
            # them. Force-including them again would produce duplicate paths.
            if sdist_frontend.exists() or sdist_default_env.exists():
                self._validate_frontend(sdist_frontend)
                self._validate_default_env(sdist_default_env)
                return

            self._validate_frontend(checkout_frontend)
            self._validate_default_env(checkout_default_env)
            build_data["force_include"].update(
                {
                    str(checkout_frontend): "careerdesk/frontend_dist",
                    str(checkout_default_env): "careerdesk/default.env",
                }
            )
        else:  # pragma: no cover - hook is configured only for wheel/sdist
            return

    @staticmethod
    def _validate_frontend(source: Path) -> None:
        if not (source / "index.html").is_file() or not (source / "assets").is_dir():
            raise RuntimeError(
                "缺少预构建 frontend；发布前必须先运行 "
                "`cd frontend && npm ci && npm run build`。"
            )

    @staticmethod
    def _validate_default_env(source: Path) -> None:
        if not source.is_file():
            raise RuntimeError(f"缺少默认配置模板：{source}")
