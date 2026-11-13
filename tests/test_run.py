"""`run`, driven through the command line with a fake `claude`."""

import json
import time
from pathlib import Path

import yaml
from conftest import API_KEY, OAUTH_TOKEN, git, press_ctrl_c, results_folder, workspace_home

# --- Settings ------------------------------------------------------------------


def test_bad_settings_are_rejected_naming_every_problem(experiment, fake, cli, tmp_path):
    settings = experiment(
        base="no-such-commit",
        concurrency=0,
        auth="password",
        surprise=True,
        languages=["cobol"],
        variants=[
            {"name": "a", "prompt": "prompts/missing.md", "model": "m", "effort": "huge"},
            {"name": "a", "prompt": "prompts/build.md"},
        ],
    )
    code, out, err = cli("run", settings)
    assert code == 2
    for expected in [
        "concurrency: Input should be greater than or equal to 1",
        "auth: Input should be 'api-key' or 'subscription'",
        "surprise: unknown field",
        "variants.0.effort",
        "variants.1.model: required field is missing",
    ]:
        assert expected in err
    assert not (settings.parent / "results").exists()
    assert fake.calls(judge=None) == []


def test_settings_that_parse_but_point_at_nothing_are_rejected(experiment, cli):
    settings = experiment(
        base="no-such-commit",
        languages=["cobol"],
        variants=[
            {"name": "a", "prompt": "prompts/missing.md", "model": "m"},
            {"name": "a", "prompt": "prompts/build.md", "model": "m", "config": "nope"},
        ],
    )
    code, _, err = cli("run", settings)
    assert code == 2
    for expected in [
        "base: commit 'no-such-commit' not found",
        "languages.0: no preset named 'cobol'",
        "variants.0.prompt: file not found",
        "variants: name 'a' is used more than once",
        "variants.1.config: folder not found",
    ]:
        assert expected in err


def test_a_prompt_using_the_plan_needs_a_plan(experiment, cli):
    code, _, err = cli("run", experiment(plan=None))
    assert code == 2
    assert "uses {{plan}} but the settings name no plan" in err


# --- One Trial end to end --------------------------------------------------------


def test_one_trial_end_to_end(experiment, fake, cli, target_repo):
    head_before = git(target_repo, "rev-parse", "HEAD")
    status_before = git(target_repo, "status", "--porcelain", "--ignored")
    settings = experiment()

    code, out, _ = cli("run", settings)

    assert code == 0, out
    assert out.index("WARNING: build-bench starts coding agents") < out.index("[start] alpha-1")
    assert "[done]  alpha-1 finished in" in out
    assert "\x1b[" not in out  # not a terminal, so no live display
    folder = results_folder(settings)
    trial = folder / "trials" / "alpha-1"

    # The developer's checkout is untouched.
    assert git(target_repo, "rev-parse", "HEAD") == head_before
    assert git(target_repo, "status", "--porcelain", "--ignored") == status_before

    # The agent ran in a clone at the base commit with no remote and the plan copied in.
    [call] = fake.calls()
    workspace = Path(call["cwd"])
    assert workspace == workspace_home(trial) / "repo"
    assert git(workspace, "remote") == ""
    base = git(target_repo, "rev-parse", "HEAD~1").strip()
    assert git(workspace, "rev-parse", "HEAD~1").strip() == base  # the fake added one commit
    assert ".scratch/plan/PLAN.md" in call["cwd_files"]
    assert "later.txt" not in call["cwd_files"]
    assert call["prompt"] == "Build the plan in .scratch/plan. Tick each box.\n"

    # Model, effort and bypassed permissions, as docs/headless-trials.md says.
    argv = call["argv"]
    assert argv[argv.index("--model") + 1] == "claude-opus-5-5"
    assert argv[argv.index("--effort") + 1] == "high"
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"
    assert {"-p", "--verbose"} <= set(argv)
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert call["env"]["CLAUDECODE"] is None and call["env"]["CLAUDE_CODE_SESSION_ID"] is None

    # What the results folder holds.
    for name in [
        "status.json",
        "prompt.md",
        "stream.jsonl",
        "summary.md",
        "transcript/main.jsonl",
        "timings.json",
        "diff.patch",
        "plan.diff",
    ]:
        assert (trial / name).is_file(), name
    assert len(list((trial / "transcript" / "subagents").glob("*.jsonl"))) == 2
    assert (folder / "settings.yml").read_text() == settings.read_text()
    assert json.loads((trial / "status.json").read_text())["status"] == "finished"

    diff = (trial / "diff.patch").read_text()
    assert "fake_committed.txt" in diff and "fake_uncommitted.txt" in diff
    assert "PLAN.md" not in diff
    plan_diff = (trial / "plan.diff").read_text()
    assert "-- [ ] add mul" in plan_diff and "+- [x] add mul" in plan_diff
    assert (trial / "summary.md").read_text() == "Fake summary: all done."


def test_an_experiment_with_no_plan_runs(experiment, fake, cli):
    settings = experiment(
        plan=None,
        variants=[{"name": "solo", "prompt": "prompts/plain.md", "model": "claude-sonnet-5-5"}],
    )
    code, out, _ = cli("run", settings)
    assert code == 0, out
    [call] = fake.calls()
    assert call["prompt"] == "Add a mul function.\n"
    assert "--effort" not in call["argv"]
    assert (results_folder(settings) / "trials" / "solo-1" / "plan.diff").read_text() == ""


# --- Skills ------------------------------------------------------------------------


def make_skill(folder: Path, name: str, body: str = "v1") -> Path:
    skill = folder / name
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(f"---\nname: {name}\ndescription: test\n---\n{body}\n")
    return skill


def test_a_variants_skills_are_added_to_its_config(experiment, fake, cli, tmp_path):
    exp = experiment().parent
    make_skill(exp / "skills", "build-it")
    make_skill(tmp_path / "home-skills", "review-it")
    (exp / "skills" / "build-it" / "scripts").mkdir()
    (exp / "skills" / "build-it" / "scripts" / "go.sh").write_text("echo go\n")
    (exp / "configs" / "base" / "skills" / "own").mkdir(parents=True)
    (exp / "configs" / "base" / "skills" / "own" / "SKILL.md").write_text("own\n")
    settings = experiment(
        variants=[
            {
                "name": "skilled",
                "prompt": "prompts/build.md",
                "model": "m",
                "config": "configs/base",
                "skills": ["skills/build-it", str(tmp_path / "home-skills" / "review-it")],
            },
            {"name": "plain", "prompt": "prompts/build.md", "model": "m"},
        ]
    )
    assert cli("run", settings)[0] == 0
    folder = results_folder(settings)
    skills = workspace_home(folder / "trials" / "skilled-1") / "config" / "skills"
    assert sorted(p.name for p in skills.iterdir()) == ["build-it", "own", "review-it"]
    assert (skills / "build-it" / "scripts" / "go.sh").read_text() == "echo go\n"
    assert not (workspace_home(folder / "trials" / "plain-1") / "config" / "skills").exists()
    # The results folder keeps the skills each Variant ran with.
    saved = folder / "skills" / "skilled"
    assert sorted(p.name for p in saved.iterdir()) == ["build-it", "review-it"]
    assert "v1" in (saved / "build-it" / "SKILL.md").read_text()
    # The developer's own folders are copied, never changed.
    assert sorted(p.name for p in (exp / "configs" / "base" / "skills").iterdir()) == ["own"]


def test_resumed_trials_use_the_skills_saved_when_the_experiment_started(experiment, fake, cli):
    exp = experiment().parent
    make_skill(exp / "skills", "build-it", body="first")
    fake.set(per_cwd={"/v-2/": "fail_once"})
    settings = experiment(
        variants=[
            {
                "name": "v",
                "prompt": "prompts/build.md",
                "model": "m",
                "repeats": 2,
                "skills": ["skills/build-it"],
            }
        ]
    )
    assert cli("run", settings)[0] == 1
    make_skill(exp / "skills", "build-it", body="second")
    assert cli("run", settings, "--resume")[0] == 0
    folder = results_folder(settings)
    skill = workspace_home(folder / "trials" / "v-2") / "config" / "skills" / "build-it"
    assert "first" in (skill / "SKILL.md").read_text()


def test_skills_never_land_in_a_linked_skills_folder(experiment, fake, cli):
    exp = experiment().parent
    make_skill(exp / "skills", "build-it")
    shared = exp / "shared-skills"
    make_skill(shared, "own")
    (exp / "configs" / "base").mkdir(parents=True)
    # A relative link, as a repository would commit it.
    (exp / "configs" / "base" / "skills").symlink_to(Path("..") / ".." / "shared-skills")
    settings = experiment(
        variants=[
            {
                "name": "v",
                "prompt": "prompts/build.md",
                "model": "m",
                "config": "configs/base",
                "skills": ["skills/build-it"],
            }
        ]
    )
    assert cli("run", settings)[0] == 0
    skills = workspace_home(results_folder(settings) / "trials" / "v-1") / "config" / "skills"
    assert sorted(p.name for p in skills.iterdir()) == ["build-it", "own"]
    assert sorted(p.name for p in shared.iterdir()) == ["own"]


def test_bad_skills_are_rejected(experiment, cli):
    exp = experiment().parent
    make_skill(exp / "skills", "fine")
    make_skill(exp / "other", "fine")
    make_skill(exp / "skills", "taken")
    (exp / "skills" / "no-skill-md").mkdir(parents=True)
    (exp / "configs" / "base" / "skills" / "taken").mkdir(parents=True)
    settings = experiment(
        variants=[
            {
                "name": "a",
                "prompt": "prompts/build.md",
                "model": "m",
                "config": "configs/base",
                "skills": [
                    "skills/missing",
                    "skills/no-skill-md",
                    "skills/fine",
                    "other/fine",
                    "skills/taken",
                ],
            }
        ]
    )
    code, _, err = cli("run", settings)
    assert code == 2
    for expected in [
        "variants.0.skills.0: folder not found",
        "variants.0.skills.1: no SKILL.md in",
        "variants.0.skills: skill 'fine' is listed more than once",
        "variants.0.skills.4: the config folder already has a skill named 'taken'",
    ]:
        assert expected in err


# --- Config, login and limits ----------------------------------------------------


def test_each_trial_gets_a_fresh_copy_of_its_variants_config(experiment, fake, cli):
    exp = experiment().parent
    for name in ("one", "two"):
        (exp / "configs" / name).mkdir(parents=True)
        (exp / "configs" / name / f"marker-{name}").write_text(name)
    settings = experiment(
        variants=[
            {
                "name": "v1",
                "prompt": "prompts/build.md",
                "model": "m",
                "config": "configs/one",
                "repeats": 2,
            },
            {"name": "v2", "prompt": "prompts/build.md", "model": "m", "config": "configs/two"},
        ]
    )
    assert cli("run", settings)[0] == 0
    calls = {Path(c["cwd"]).parent.name: c for c in fake.calls()}
    assert calls["v1-1"]["config_files"] == ["marker-one"]
    assert calls["v1-2"]["config_files"] == ["marker-one"]
    assert calls["v2-1"]["config_files"] == ["marker-two"]
    dirs = {c["env"]["CLAUDE_CONFIG_DIR"] for c in calls.values()}
    assert len(dirs) == 3
    for trial, call in calls.items():
        assert call["env"]["CLAUDE_CONFIG_DIR"].endswith(f"/{trial}/config")
    # The developer's own config is copied, never changed.
    assert sorted(p.name for p in (exp / "configs" / "one").iterdir()) == ["marker-one"]


def test_api_key_login(experiment, fake, cli):
    settings = experiment(auth="api-key")
    assert cli("run", settings)[0] == 0
    [call] = fake.calls()
    assert call["env"]["ANTHROPIC_API_KEY"] == API_KEY
    assert call["env"]["CLAUDE_CODE_OAUTH_TOKEN"] is None
    folder = results_folder(settings)
    assert json.loads((folder / "experiment.json").read_text())["auth"] == "api-key"


def test_subscription_login_from_a_named_variable(experiment, fake, cli, monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "sk-ant-oat01-from-my-own-variable")
    settings = experiment(auth="subscription", auth_env="MY_TOKEN")
    assert cli("run", settings)[0] == 0
    [call] = fake.calls()
    assert call["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-from-my-own-variable"
    assert call["env"]["ANTHROPIC_API_KEY"] is None
    folder = results_folder(settings)
    assert json.loads((folder / "experiment.json").read_text())["auth"] == "subscription"
    assert json.loads((folder / "trials/alpha-1/status.json").read_text())["auth"] == (
        "subscription"
    )


def test_a_missing_login_variable_stops_before_the_first_trial(experiment, fake, cli, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN")
    code, _, err = cli("run", experiment(auth="subscription"))
    assert code == 2
    assert "missing environment variable(s): CLAUDE_CODE_OAUTH_TOKEN" in err
    assert fake.calls(judge=None) == []


def test_no_results_file_holds_a_key(experiment, fake, cli, monkeypatch):
    fake.set(summary=f"I found the key {API_KEY} in the environment.")
    settings = experiment()
    assert cli("run", settings)[0] == 0
    folder = results_folder(settings)
    files = [p for p in folder.rglob("*") if p.is_file() and ".git" not in p.parts]
    assert files
    for path in files:
        data = path.read_bytes()
        assert API_KEY.encode() not in data, path
        assert OAUTH_TOKEN.encode() not in data, path
    assert "[redacted]" in (folder / "trials/alpha-1/summary.md").read_text()


def test_a_pinned_version_that_differs_stops_the_run(experiment, fake, cli):
    fake.set(version="2.0.1")
    code, _, err = cli("run", experiment(claude_version="2.1.319"))
    assert code == 2
    assert "Claude Code 2.0.1 is installed but the settings pin 2.1.319" in err
    assert fake.calls(judge=None) == []


def test_a_pinned_version_that_matches_runs_and_is_saved(experiment, fake, cli):
    fake.set(version="2.1.319")
    settings = experiment(claude_version="2.1.319")
    assert cli("run", settings)[0] == 0
    folder = results_folder(settings)
    assert json.loads((folder / "experiment.json").read_text())["claude_version"] == "2.1.319"
    status = json.loads((folder / "trials/alpha-1/status.json").read_text())
    assert status["claude_version"] == "2.1.319"


def test_a_trial_past_its_time_limit_is_stopped_and_failed(experiment, fake, cli):
    fake.set(mode="hang")
    settings = experiment(limits={"timeout_minutes": 0.02})
    started = time.time()
    code, out, _ = cli("run", settings)
    assert time.time() - started < 30
    assert code == 1
    status = json.loads((results_folder(settings) / "trials/alpha-1/status.json").read_text())
    assert status["status"] == "failed"
    assert status["reason"] == "timeout: stopped after 0.02 minutes"
    assert "[fail]  alpha-1 timeout" in out


def test_ctrl_c_stops_the_agents_quietly_and_resume_finishes_the_rest(experiment, fake, cli):
    fake.set(mode="hang")
    settings = experiment(
        concurrency=2,
        variants=[{"name": "alpha", "prompt": "prompts/build.md", "model": "m", "repeats": 3}],
    )
    press_ctrl_c(lambda: len(fake.calls()) == 2)
    started = time.time()

    code, out, err = cli("run", settings)

    # The hanging agents were stopped, not waited for.
    assert time.time() - started < 15
    assert code == 130
    assert "Ctrl+C stops the run; `build-bench run --resume` finishes it later." in out
    assert "Traceback" not in out + err and "KeyboardInterrupt" not in out + err
    assert "`build-bench run --resume` finishes the Trials left.\nStopped." in err
    trials = results_folder(settings) / "trials"
    for trial in ("alpha-1", "alpha-2"):
        status = json.loads((trials / trial / "status.json").read_text())
        assert status["status"] == "failed"
        assert status["reason"] == "stopped: build-bench was stopped (Ctrl+C)"
        assert f"[fail]  {trial} stopped: build-bench was stopped (Ctrl+C)" in out
    # The Trial still waiting never started.
    assert not (trials / "alpha-3").exists()
    assert len(fake.calls()) == 2

    fake.set()
    code, out, _ = cli("run", settings, "--resume")
    assert code == 0, out
    assert len(fake.calls()) == 5


def test_a_second_ctrl_c_kills_an_agent_that_will_not_stop(experiment, fake, cli):
    fake.set(mode="stubborn")
    settings = experiment()
    press_ctrl_c(lambda: len(fake.calls()) == 1, times=2)
    started = time.time()

    code, _, err = cli("run", settings)

    # The agent ignores SIGTERM; without the second Ctrl+C this takes 10 seconds.
    assert time.time() - started < 8
    assert code == 130
    assert "Traceback" not in err
    status = json.loads((results_folder(settings) / "trials/alpha-1/status.json").read_text())
    assert status["reason"] == "stopped: build-bench was stopped (Ctrl+C)"


def test_a_trial_that_hits_its_budget_is_failed(experiment, fake, cli):
    fake.set(mode="budget")
    settings = experiment(limits={"max_budget_usd": 0.5})
    assert cli("run", settings)[0] == 1
    [call] = fake.calls()
    assert call["argv"][call["argv"].index("--max-budget-usd") + 1] == "0.5"
    status = json.loads((results_folder(settings) / "trials/alpha-1/status.json").read_text())
    assert status["reason"] == "budget: reached the spending limit of $0.5"


# --- Many Trials -------------------------------------------------------------------


def two_by_three(experiment, **extra):
    return experiment(
        variants=[
            {"name": "v1", "prompt": "prompts/build.md", "model": "m1", "repeats": 3},
            {"name": "v2", "prompt": "prompts/build.md", "model": "m2", "repeats": 3},
        ],
        **extra,
    )


def test_repeats_and_concurrency(experiment, fake, cli):
    fake.set(sleep=0.6)
    settings = two_by_three(experiment, concurrency=2)
    code, out, _ = cli("run", settings)
    assert code == 0, out
    folder = results_folder(settings)
    assert sorted(p.name for p in (folder / "trials").iterdir()) == [
        "v1-1",
        "v1-2",
        "v1-3",
        "v2-1",
        "v2-2",
        "v2-3",
    ]
    calls = fake.calls()
    assert len(calls) == 6
    most = max(sum(1 for c in calls if c["start"] <= x["start"] < c["end"]) for x in calls)
    assert most == 2
    active = [
        json.loads((folder / "trials" / t / "timings.json").read_text())["max_active"]
        for t in ("v1-1", "v1-2", "v1-3", "v2-1", "v2-2", "v2-3")
    ]
    assert max(active) == 2 and all(a <= 2 for a in active)
    for trial in ("v1-1", "v2-3"):
        assert f"[start] {trial}" in out and f"[done]  {trial} finished" in out


def test_pre_build_runs_first_and_is_not_timed_or_diffed(experiment, fake, cli):
    settings = experiment(
        pre_build=[
            "sleep 1",
            'mkdir -p build && echo cached > build/cache.txt && python3 -c "import time; print(time.time())" > build/stamp',
        ]
    )
    assert cli("run", settings)[0] == 0
    trial = results_folder(settings) / "trials" / "alpha-1"
    [call] = fake.calls()
    stamp = float((workspace_home(trial) / "repo/build/stamp").read_text())
    assert stamp <= call["start"]
    assert "build/cache.txt" in call["cwd_files"]
    timings = json.loads((trial / "timings.json").read_text())
    assert timings["pre_build_seconds"] >= 1
    assert timings["agent_seconds"] < timings["pre_build_seconds"] + 5
    assert timings["agent_started_at"] >= stamp
    assert "build/" not in (trial / "diff.patch").read_text()


def test_a_failing_pre_build_step_fails_the_trial_before_the_agent(experiment, fake, cli):
    settings = experiment(pre_build=["echo preparing", "exit 7"])
    code, out, _ = cli("run", settings)
    assert code == 1
    status = json.loads((results_folder(settings) / "trials/alpha-1/status.json").read_text())
    assert status["status"] == "failed"
    assert status["reason"] == "pre_build: `exit 7` exited with code 7"
    assert fake.calls(judge=None) == []
    assert "[fail]  alpha-1 pre_build" in out


def test_a_crash_fails_one_trial_without_retry_or_stopping_others(experiment, fake, cli):
    fake.set(per_cwd={"/v1-2/": "crash"})
    settings = two_by_three(experiment, concurrency=3)
    code, out, _ = cli("run", settings)
    assert code == 1
    assert len(fake.calls()) == 6
    folder = results_folder(settings)
    statuses = {
        p.parent.name: json.loads(p.read_text()) for p in (folder / "trials").glob("*/status.json")
    }
    assert statuses["v1-2"]["status"] == "failed"
    assert statuses["v1-2"]["reason"].startswith("crashed: exit code 3")
    assert "fake claude crashed on purpose" in statuses["v1-2"]["reason"]
    assert [t for t, s in statuses.items() if s["status"] == "finished"] != []
    assert sum(s["status"] == "finished" for s in statuses.values()) == 5
    assert "[fail]  v1-2 crashed" in out


def test_running_again_finishes_only_missing_and_failed_trials(experiment, fake, cli):
    fake.set(per_cwd={"/v2-1/": "fail_once"})
    settings = two_by_three(experiment)
    assert cli("run", settings)[0] == 1
    assert len(fake.calls()) == 6
    folder = results_folder(settings)
    # Lose one finished Trial entirely, as if the run was interrupted.
    import shutil

    shutil.rmtree(folder / "trials" / "v1-3")

    code, out, _ = cli("run", settings, "--resume")
    assert code == 0, out
    calls = fake.calls()
    assert len(calls) == 8
    rerun = sorted(Path(c["cwd"]).parent.name for c in calls[6:])
    assert rerun == ["v1-3", "v2-1"]
    assert "Skipping 4 finished Trial(s)." in out
    assert sorted(p.name for p in folder.parent.iterdir()) == [folder.name]
    statuses = [json.loads(p.read_text()) for p in (folder / "trials").glob("*/status.json")]
    assert all(s["status"] == "finished" for s in statuses)


def test_run_into_a_named_results_folder(experiment, fake, cli):
    settings = experiment()
    assert cli("run", settings)[0] == 0
    folder = results_folder(settings)
    assert cli("run", settings, "--results", folder)[0] == 0
    assert len(fake.calls()) == 1
    assert yaml.safe_load((folder / "settings.yml").read_text())["name"] == "demo"


def test_workspaces_live_outside_the_results_folder(experiment, fake, cli, cache_home):
    # An Experiment kept inside a repository whose CLAUDE.md must not reach the Trials.
    settings = experiment()
    (settings.parent / "CLAUDE.md").write_text("Reply with PINEAPPLE.\n")
    code, out, _ = cli("run", settings)
    assert code == 0
    assert "Claude Code also reads instruction files" not in out
    folder = results_folder(settings)
    [call] = fake.calls()
    home = workspace_home(folder / "trials" / "alpha-1")
    assert Path(call["cwd"]) == home / "repo"
    assert home.is_relative_to(cache_home / "build-bench" / "workspaces")
    assert not Path(call["cwd"]).is_relative_to(settings.parent)
    assert not (folder / "trials" / "alpha-1" / "workspace").exists()


def test_instruction_files_above_the_workspaces_are_warned_about(experiment, fake, cli, cache_home):
    cache_home.mkdir()
    (cache_home / "CLAUDE.md").write_text("Reply with PINEAPPLE.\n")
    code, out, _ = cli("run", experiment())
    assert code == 0
    assert "Claude Code also reads instruction files in folders above" in out
    assert f"  {cache_home / 'CLAUDE.md'}" in out


def test_results_go_in_a_git_ignored_folder_beside_the_settings(experiment, fake, cli):
    settings = experiment()
    assert cli("run", settings)[0] == 0
    root = settings.parent / ".bench" / "results"
    assert (root / ".gitignore").read_text() == "*\n"
    assert results_folder(settings).parent == root / "demo"


def test_the_results_setting_moves_the_results(experiment, fake, cli, tmp_path):
    settings = experiment(results="../elsewhere")
    assert cli("run", settings)[0] == 0
    [folder] = (tmp_path / "elsewhere" / "demo").iterdir()
    assert (folder / "trials" / "alpha-1" / "status.json").is_file()
    assert not (settings.parent / ".bench").exists()


def test_results_beside_the_settings_never_git_ignore_the_settings(experiment, fake, cli):
    settings = experiment(results=".")
    assert cli("run", settings)[0] == 0
    assert (settings.parent / "demo").is_dir()
    assert not (settings.parent / ".gitignore").exists()


def test_a_benchrc_above_the_current_folder_is_the_default_settings(
    experiment, fake, cli, monkeypatch
):
    settings = experiment()
    benchrc = settings.parent / ".benchrc"
    settings.rename(benchrc)
    below = settings.parent / "some" / "folder"
    below.mkdir(parents=True)
    monkeypatch.chdir(below)
    code, out, _ = cli("run")
    assert code == 0, out
    [folder] = (settings.parent / ".bench" / "results" / "demo").iterdir()
    assert json.loads((folder / "experiment.json").read_text())["settings_file"] == str(benchrc)
    assert cli("report")[0] == 0
    assert (folder / "report.md").is_file()


def test_no_settings_and_no_benchrc_is_unusable(cli, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code, _, err = cli("all")
    assert code == 2
    assert "no settings file given and no .benchrc" in err


def test_a_tracked_plan_is_kept_out_of_the_code_diff(experiment, fake, cli, target_repo):
    plan = target_repo / "docs" / "plan"
    plan.mkdir(parents=True)
    (plan / "PLAN.md").write_text("- [ ] add mul\n")
    git(target_repo, "add", "docs")
    git(target_repo, "commit", "-q", "-m", "tracked plan")
    settings = experiment(base="HEAD", plan="docs/plan")
    assert cli("run", settings)[0] == 0
    trial = results_folder(settings) / "trials" / "alpha-1"
    for name in ("diff.patch", "diff-full.patch"):
        diff = (trial / name).read_text()
        assert "PLAN.md" not in diff, name
        assert "fake_committed.txt" in diff
    assert "+- [x] add mul" in (trial / "plan.diff").read_text()


def test_nothing_past_the_base_reaches_the_trial(experiment, fake, cli, target_repo):
    git(target_repo, "tag", "v1.0")  # on the commit after the base
    later = git(target_repo, "rev-parse", "HEAD").strip()
    settings = experiment()
    assert cli("run", settings)[0] == 0
    workspace = workspace_home(results_folder(settings) / "trials" / "alpha-1") / "repo"
    assert git(workspace, "tag", "--list") == ""
    assert git(workspace, "branch", "--list", "--format=%(refname:short)") == "build-bench\n"
    reflog = git(workspace, "reflog", "--all", "--format=%H")
    objects = git(workspace, "cat-file", "--batch-all-objects", "--batch-check")
    assert later not in reflog and later not in objects
    assert git(target_repo, "tag", "--list") == "v1.0\n"
