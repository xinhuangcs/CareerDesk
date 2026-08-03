"""Load only bundled trusted skills; never admit arbitrary local paths."""

from pathlib import Path

from agentmaker import Skill, SkillLoader

from ...platform.locale import DEFAULT_OUTPUT_LOCALE, OutputLocale


DEFAULT_SKILL_NAMES = (
    "prepare-for-interview",
    "emotional-support",
)


class TrustedSkillCatalog:
    """Application security wrapper around AgentMaker SkillLoader.

    Fixes the resource root and allowlist, failing on missing/unregistered skills so a
    model cannot expand loadable content through names or paths.
    """

    def __init__(self, skills_root: Path | None = None,
                 allowed_names: tuple[str, ...] = DEFAULT_SKILL_NAMES):
        root = skills_root or Path(__file__).resolve().parents[1] / "skills"
        self._root = root
        self._loader = SkillLoader(str(root))
        self._allowed_names = tuple(allowed_names)

    @property
    def names(self) -> tuple[str, ...]:
        return self._allowed_names

    def discover(self) -> tuple[Skill, ...]:
        discovered = {skill.name: skill for skill in self._loader.discover()}
        expected = set(self._allowed_names)
        missing = expected - discovered.keys()
        unexpected = discovered.keys() - expected
        if missing or unexpected:
            details = []
            if missing:
                details.append(f"missing={sorted(missing)}")
            if unexpected:
                details.append(f"unexpected={sorted(unexpected)}")
            raise ValueError("Skill catalog does not match the trusted allowlist: " + ", ".join(details))
        return tuple(discovered[name] for name in self._allowed_names)

    def _english_skill(self, name: str) -> tuple[str, str]:
        path = self._root / name / "SKILL.en.md"
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as error:
            raise ValueError(f"Missing English skill resource: {name}") from error
        parts = text.split("---", 2)
        if len(parts) != 3 or parts[0].strip():
            raise ValueError(f"Invalid English skill front matter: {name}")
        metadata: dict[str, str] = {}
        for line in parts[1].splitlines():
            key, separator, value = line.partition(":")
            if separator:
                metadata[key.strip()] = value.strip()
        if metadata.get("name") != name or not metadata.get("description"):
            raise ValueError(f"English skill identity does not match its directory: {name}")
        body = parts[2].strip()
        if not body:
            raise ValueError(f"English skill body is empty: {name}")
        return metadata["description"], body

    def catalog(self, output_locale: OutputLocale = DEFAULT_OUTPUT_LOCALE) -> str:
        """Return the lightweight resident catalog of names and trigger descriptions."""
        if output_locale == "en":
            return "\n".join(
                f"- {name}: {self._english_skill(name)[0]}"
                for name in self._allowed_names
            )
        return "\n".join(f"- {skill.name}: {skill.description}" for skill in self.discover())

    def load(
        self,
        name: str,
        output_locale: OutputLocale = DEFAULT_OUTPUT_LOCALE,
    ) -> str | None:
        """Read by exact skill ID, rejecting unknown names and path-shaped input."""
        if name not in self._allowed_names:
            return None
        if output_locale == "en":
            return self._english_skill(name)[1]
        return self._loader.load(name)
