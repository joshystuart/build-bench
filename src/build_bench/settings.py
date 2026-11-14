"""Load and check an Experiment's settings file."""

from __future__ import annotations

import os
import subprocess
from importlib import resources
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"

#: The environment variable each login type reads when the settings name none.
DEFAULT_AUTH_ENV = {
    "api-key": "ANTHROPIC_API_KEY",
    "subscription": "CLAUDE_CODE_OAUTH_TOKEN",
}


class SettingsError(Exception):
    """The settings file is unusable. ``problems`` names every problem found."""

    def __init__(self, problems: list[str]):
        super().__init__("\n".join(problems))
        self.problems = problems


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Limits(_Strict):
    timeout_minutes: float | None = Field(default=None, gt=0)
    max_budget_usd: float | None = Field(default=None, gt=0)


class Price(_Strict):
    """US dollars per million tokens."""

    input: float = Field(ge=0)
    output: float = Field(ge=0)
    cache_write_5m: float | None = Field(default=None, ge=0)
    cache_write_1h: float | None = Field(default=None, ge=0)
    cache_read: float | None = Field(default=None, ge=0)


#: The coding agents build-bench can start.
AgentKind = Literal["claude", "copilot"]


class Variant(_Strict):
    name: str = Field(pattern=NAME_PATTERN)
    #: The agent that builds: Claude Code, or the GitHub Copilot CLI.
    agent: AgentKind = "claude"
    prompt: Path
    model: str
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    config: Path | None = None
    skills: list[Path] = []
    repeats: int = Field(default=1, ge=1)


class Compare(_Strict):
    #: The most pairs of Trials this Judge compares, spread over the Variants.
    max_pairs: int = Field(ge=1)


class Judge(_Strict):
    name: str = Field(pattern=NAME_PATTERN)
    #: An Agent Judge (`claude` or `copilot`), or one model call (`api`).
    type: Literal["claude", "copilot", "api"]
    model: str
    prompt: Path | None = None
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    api_key_env: str | None = None
    limits: Limits | None = None
    #: Set to have this Judge also say which of two Trials is better. Off when left out.
    compare: Compare | None = None


class Settings(_Strict):
    name: str = Field(pattern=NAME_PATTERN)
    repo: Path
    base: str
    plan: Path | None = None
    results: Path = Path(".bench/results")
    pre_build: list[str] = []
    checks: str | None = None
    concurrency: int = Field(default=1, ge=1)
    #: Claude Code's login. Needed only when a Variant or Judge is Claude Code;
    #: the Copilot CLI finds its own login.
    auth: Literal["api-key", "subscription"] | None = None
    auth_env: str | None = None
    claude_version: str | None = None
    copilot_version: str | None = None
    limits: Limits = Limits()
    languages: list[str] = []
    test_paths: list[str] = []
    ignore_paths: list[str] = []
    prices: dict[str, Price] = {}
    variants: list[Variant] = Field(min_length=1)
    judges: list[Judge] = []
    #: The Judge that merges Findings into Defects; the first Judge when not named.
    merge_judge: str | None = None

    # Filled in by load(), never read from the file.
    path: Path | None = Field(default=None, exclude=True)
    base_sha: str | None = Field(default=None, exclude=True)
    plan_rel: str | None = Field(default=None, exclude=True)

    @field_validator("claude_version", "copilot_version", "base", mode="before")
    @classmethod
    def _as_text(cls, value: Any) -> Any:
        # YAML reads 2.1 as a number and an all-digit commit as an integer.
        return str(value) if isinstance(value, int | float) else value

    @property
    def folder(self) -> Path:
        assert self.path is not None
        return self.path.parent

    @property
    def auth_variable(self) -> str:
        assert self.auth is not None
        return self.auth_env or DEFAULT_AUTH_ENV[self.auth]

    def agents(self, *, judges: bool) -> list[str]:
        """The agents the Variants start and, with `judges`, the Agent Judges too."""
        used: list[str] = [v.agent for v in self.variants]
        if judges:
            used += [j.type for j in self.judges if j.type != "api"]
        return list(dict.fromkeys(used))

    def version_pin(self, agent: str) -> str | None:
        return self.claude_version if agent == "claude" else self.copilot_version

    def agent_login(self, agent: str) -> tuple[str, str] | None:
        """The login to hand an agent: Claude Code's variable and its value. The
        Copilot CLI is handed none; it finds its own."""
        if agent != "claude":
            return None
        assert self.auth is not None
        return DEFAULT_AUTH_ENV[self.auth], os.environ[self.auth_variable]

    def judge_limits(self, judge: Judge) -> Limits:
        """A Judge's own limits win; anything it leaves out comes from the Experiment."""
        own = judge.limits or Limits()
        return Limits(
            timeout_minutes=own.timeout_minutes or self.limits.timeout_minutes,
            max_budget_usd=own.max_budget_usd or self.limits.max_budget_usd,
        )


#: The settings file the commands use when none is named.
DEFAULT_NAME = ".benchrc"


def find_default(start: Path) -> Path:
    """The nearest `.benchrc` in `start` or a folder above it."""
    for folder in [start, *start.parents]:
        if (folder / DEFAULT_NAME).is_file():
            return folder / DEFAULT_NAME
    raise SettingsError([f"no settings file given and no {DEFAULT_NAME} in {start} or above it"])


def load(path: str | Path) -> Settings:
    """Read the settings file, resolve its paths and check everything that can be checked."""
    path = Path(path).resolve()
    try:
        raw = yaml.safe_load(path.read_text())
    except FileNotFoundError:
        raise SettingsError([f"{path}: file not found"]) from None
    except yaml.YAMLError as error:
        raise SettingsError([f"{path}: not valid YAML: {error}"]) from None
    if not isinstance(raw, dict):
        raise SettingsError([f"{path}: expected a mapping of settings"])

    try:
        settings = Settings.model_validate(raw)
    except ValidationError as error:
        raise SettingsError([_describe(path, e) for e in error.errors()]) from None

    settings.path = path
    problems = _check(settings)
    if problems:
        raise SettingsError([f"{path}: {p}" for p in problems])
    return settings


def _describe(path: Path, error: Any) -> str:
    where = ".".join(str(part) for part in error["loc"]) or "(top level)"
    message = error["msg"]
    if error["type"] == "missing":
        message = "required field is missing"
    elif error["type"] == "extra_forbidden":
        message = "unknown field"
    return f"{path}: {where}: {message}"


def _check(s: Settings) -> list[str]:
    problems: list[str] = []
    folder = s.folder

    s.repo = (folder / s.repo).resolve()
    s.results = (folder / s.results).resolve()
    if not (s.repo / ".git").exists():
        problems.append(f"repo: not a git repository: {s.repo}")
    else:
        found = subprocess.run(
            ["git", "-C", str(s.repo), "rev-parse", "--verify", "--quiet", f"{s.base}^{{commit}}"],
            capture_output=True,
            text=True,
        )
        if found.returncode != 0:
            problems.append(f"base: commit {s.base!r} not found in {s.repo}")
        else:
            s.base_sha = found.stdout.strip()

    if s.plan is not None:
        # The plan is copied to the same relative path inside each Trial's clone,
        # so it is named relative to the target repository.
        plan = (s.repo / s.plan).resolve()
        if not plan.is_dir():
            problems.append(f"plan: folder not found: {plan}")
        elif not plan.is_relative_to(s.repo) or plan == s.repo:
            problems.append(f"plan: must be a folder inside the repository: {plan}")
        else:
            s.plan = plan
            s.plan_rel = plan.relative_to(s.repo).as_posix()

    names = [v.name for v in s.variants]
    for name in sorted({n for n in names if names.count(n) > 1}):
        problems.append(f"variants: name {name!r} is used more than once")
    for i, variant in enumerate(s.variants):
        variant.prompt = (folder / variant.prompt).resolve()
        if not variant.prompt.is_file():
            problems.append(f"variants.{i}.prompt: file not found: {variant.prompt}")
        elif "{{plan}}" in variant.prompt.read_text() and s.plan is None:
            problems.append(f"variants.{i}.prompt: uses {{{{plan}}}} but the settings name no plan")
        if variant.config is not None:
            variant.config = (folder / variant.config).resolve()
            if not variant.config.is_dir():
                problems.append(f"variants.{i}.config: folder not found: {variant.config}")
        problems += _check_skills(variant, f"variants.{i}", folder)

    uses_claude = [f"variants.{i}" for i, v in enumerate(s.variants) if v.agent == "claude"]
    uses_claude += [f"judges.{i}" for i, j in enumerate(s.judges) if j.type == "claude"]
    if s.auth is None and uses_claude:
        problems.append(f"auth: required, because {uses_claude[0]} is Claude Code")

    judge_names = [j.name for j in s.judges]
    for name in sorted({n for n in judge_names if judge_names.count(n) > 1}):
        problems.append(f"judges: name {name!r} is used more than once")
    if s.merge_judge is not None and s.merge_judge not in judge_names:
        problems.append(f"merge_judge: no judge named {s.merge_judge!r}")
    for i, judge in enumerate(s.judges):
        if judge.prompt is not None:
            judge.prompt = (folder / judge.prompt).resolve()
            if not judge.prompt.is_file():
                problems.append(f"judges.{i}.prompt: file not found: {judge.prompt}")
        if judge.type != "api" and judge.api_key_env:
            problems.append(f"judges.{i}.api_key_env: only for api judges")
        if judge.type == "api" and judge.effort:
            problems.append(f"judges.{i}.effort: only for claude and copilot judges")

    for i, language in enumerate(s.languages):
        if find_preset(s, language) is None:
            problems.append(f"languages.{i}: no preset named {language!r}")
    return problems


def _check_skills(variant: Variant, where: str, folder: Path) -> list[str]:
    """Resolve a Variant's skill folders; each becomes `skills/<folder name>` in its config."""
    problems = []
    variant.skills = [(folder / p.expanduser()).resolve() for p in variant.skills]
    names = [p.name for p in variant.skills]
    for name in sorted({n for n in names if names.count(n) > 1}):
        problems.append(f"{where}.skills: skill {name!r} is listed more than once")
    for j, skill in enumerate(variant.skills):
        if not skill.is_dir():
            problems.append(f"{where}.skills.{j}: folder not found: {skill}")
        elif not (skill / "SKILL.md").is_file():
            problems.append(f"{where}.skills.{j}: no SKILL.md in {skill}")
        elif variant.config is not None and (variant.config / "skills" / skill.name).exists():
            problems.append(
                f"{where}.skills.{j}: the config folder already has a skill named {skill.name!r}"
            )
    return problems


def find_preset(s: Settings, name: str) -> Path | None:
    """A preset is a YAML file: one beside the settings file wins over a shipped one."""
    local = s.folder / "presets" / f"{name}.yaml"
    if local.is_file():
        return local
    shipped = resources.files("build_bench") / "presets" / f"{name}.yaml"
    return Path(str(shipped)) if shipped.is_file() else None


def path_rules(s: Settings) -> tuple[list[str], list[str], bool]:
    """The test and ignore globs after presets are added, and whether Rust inline tests count."""
    tests: list[str] = []
    ignore: list[str] = []
    rust_inline = False
    for language in s.languages:
        preset_path = find_preset(s, language)
        assert preset_path is not None
        preset = yaml.safe_load(preset_path.read_text()) or {}
        tests += preset.get("test_paths", [])
        ignore += preset.get("ignore_paths", [])
        rust_inline = rust_inline or preset.get("inline_tests") == "rust"
    return tests + s.test_paths, ignore + s.ignore_paths, rust_inline


def required_env(s: Settings, *, judges: bool) -> list[str]:
    """Names of the environment variables the phase needs that are not set."""
    needed = []
    if "claude" in s.agents(judges=judges):
        needed.append(s.auth_variable)
    if judges:
        needed += [n for j in s.judges for n in judge_env(j)]
    return [n for n in dict.fromkeys(needed) if not os.environ.get(n)]


#: Models reached through a GitHub Copilot subscription. LiteLLM logs in to these
#: itself, with a code to enter at github.com, and keeps the login in its own folder.
COPILOT = "github_copilot/"


def judge_env(judge: Judge) -> list[str]:
    """The variables an API Judge needs; Agent Judges use the Experiment's login."""
    if judge.type != "api":
        return []
    if judge.api_key_env:
        return [judge.api_key_env]
    if judge.model.startswith(COPILOT):
        # No variable holds this login, and asking LiteLLM about it starts the login.
        return []
    import litellm

    found = litellm.validate_environment(model=judge.model)
    return list(found.get("missing_keys") or [])


def log_in_judges(s: Settings) -> None:
    """Log in every Judge that logs in by itself, while someone is at the terminal:
    a GitHub Copilot Judge with no saved login prints a code to enter at github.com."""
    if any(j.type == "api" and j.model.startswith(COPILOT) for j in s.judges):
        from litellm.llms.github_copilot.authenticator import Authenticator

        Authenticator().get_access_token()
