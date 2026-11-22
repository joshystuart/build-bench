"""diffs.html (the code each Trial produced, with Findings pinned on it) and `diff`."""

import json
import re
from pathlib import Path

import pytest
from conftest import git
from test_report import no_models, trial, write  # noqa: F401

from build_bench.judge import finding_key

# Only git is on the PATH, and no model may be called.
pytestmark = pytest.mark.usefixtures("no_models")

FILLER = [f"# line {n}" for n in range(3, 21)]
BASE = {
    "calc.py": ["def add(a, b):", "    return a + b", *FILLER],
    "notes.txt": ["first note", "second note"],
    "config.toml": ["name = 'old value'"],
}
A1 = {
    **BASE,
    # Line 15 changes; mul is added at the end, at lines 21 and 22.
    "calc.py": [*BASE["calc.py"][:14], "# line 15, changed", *BASE["calc.py"][15:]]
    + ["def mul(a, b):", "    return a + b"],
    "config.toml": ["name = 'new value'"],
    "src/util/helpers.py": ["def helper():", "    return '</script><b>bold</b> & <!--'", ""],
    "uv.lock": ["lock 1", "lock 2"],
}
A2 = {**BASE, "calc.py": ["def add(a, b):", "    return b + a", *FILLER[:15], "# end"]}
B3 = {
    "calc.py": [*BASE["calc.py"], "def mul(a, b):", "    return a * b"],
    "config.toml": BASE["config.toml"],
}


def diffs(tmp: Path, after: dict[str, list[str]]) -> tuple[str, str]:
    """The plain and whole-file diffs `run` would save, from real git."""
    repo = tmp / f"repo-{len(list(tmp.glob('repo-*')))}"
    repo.mkdir()
    git(repo, "init", "-q")
    for files in (BASE, after):
        for old in [p for p in repo.rglob("*") if p.is_file() and ".git" not in p.parts]:
            old.unlink()
        for path, lines in files.items():
            (repo / path).parent.mkdir(parents=True, exist_ok=True)
            (repo / path).write_text("\n".join(lines) + "\n")
        git(repo, "add", "-A")
        if files is BASE:
            git(repo, "commit", "-q", "-m", "base")
    return git(repo, "diff", "--cached"), git(repo, "diff", "--cached", "-U100000")


def finding(title: str, where: str, severity: str = "minor", detail: str = "d") -> dict:
    return {
        "title": title,
        "severity": severity,
        "category": "behaviour",
        "where": where,
        "detail": detail,
        "reproduced": False,
    }


A1_FINDINGS = [
    finding("mul adds", "`calc.py:21`", "major", "mul returns a + b."),
    finding("helper leaks", "util/helpers.py:2, calc.py line 15"),
    finding("odd name", "calc.py", "nit"),
    finding("docs missing", "README.md:3"),
    finding("far away", "calc.py:5"),
    finding("vague", "somewhere in the tests", "nit"),
]
B3_FINDING = finding("mul is wrong", "calc.py:22", "major")


@pytest.fixture
def review(experiment, tmp_path):
    """Five Trials: a-1 and b-3 built (whole-file diffs), a-2 built with only the
    plain diff (saved before diff-full.patch was kept), b-1 failed, b-2 changed nothing."""
    settings = experiment(
        variants=[
            {"name": "a", "prompt": "prompts/build.md", "model": "m1", "repeats": 2},
            {"name": "b", "prompt": "prompts/build.md", "model": "m2", "repeats": 3},
        ],
        judges=[
            {"name": "j", "type": "api", "model": "openai/gpt-5"},
            {"name": "k", "type": "api", "model": "openai/gpt-5"},
        ],
        ignore_paths=["*.lock"],
    )
    folder = settings.parent / ".bench" / "results" / "demo" / "20260101-000000"
    write(folder / "experiment.json", {"name": "demo"})
    work = tmp_path / "repos"
    work.mkdir()
    plain = {}
    for name, after in (("a-1", A1), ("a-2", A2), ("b-3", B3)):
        plain[name], full = diffs(work, after)
        trial(folder, name, diff=plain[name])
        if name != "a-2":
            write(folder / "trials" / name / "diff-full.patch", full)
    trial(folder, "b-1", status="failed", reason="crashed")
    trial(folder, "b-2", diff="")
    write(folder / "trials" / "b-2" / "diff-full.patch", "")

    ok = {"status": "ok", "note": "", "cost_usd": 0.1, "seconds": 1.0}
    write(folder / "trials" / "a-1" / "verdicts" / "j.json", {**ok, "findings": A1_FINDINGS})
    write(folder / "trials" / "b-3" / "verdicts" / "j.json", {**ok, "findings": [B3_FINDING]})
    yes = {"present": "yes", "severity": "major", "in_base": "no"}
    write(
        folder / "trials" / "a-1" / "defect-checks" / "k.json",
        {**ok, "answers": {"D1": {**yes, "where": "calc.py:21-22", "evidence": "mul adds."}}},
    )
    write(
        folder / "trials" / "b-3" / "defect-checks" / "k.json",
        {**ok, "answers": {"D1": {"present": "no", "where": "", "evidence": ""}}},
    )
    write(
        folder / "defects.json",
        {
            "judge": "j",
            "status": "ok",
            "defects": [
                {
                    "id": "D1",
                    "title": "mul does not multiply",
                    "description": "Look at mul.",
                    "category": "behaviour",
                    "severity": "major",
                    "findings": [
                        finding_key("a-1", "j", A1_FINDINGS[0]),
                        finding_key("b-3", "j", B3_FINDING),
                    ],
                }
            ],
        },
    )
    return settings, folder, plain


def page_data(folder: Path) -> dict:
    page = (folder / "diffs.html").read_text()
    found = re.search(r"<script type='application/json' id='data'>(.*?)</script>", page, re.S)
    assert found, "no data on the page"
    return json.loads(found.group(1))


def counts(diff: str) -> dict[str, tuple[int, int]]:
    """Lines added and removed per file, counted straight from a git diff."""
    out: dict[str, list[int]] = {}
    path = ""
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            path = line.split(" b/", 1)[1]
            out[path] = [0, 0]
        elif line.startswith(("+++ ", "--- ")):
            continue
        elif line.startswith("+"):
            out[path][0] += 1
        elif line.startswith("-"):
            out[path][1] += 1
    return {p: (a, r) for p, (a, r) in out.items()}


def reported(review, cli) -> dict:
    settings, folder, _ = review
    code, out, _ = cli("report", settings)
    assert code == 0
    assert f"Wrote {folder / 'diffs.html'}" in out
    return page_data(folder)


def test_the_matrix_counts_each_trials_lines_per_file(review, cli):
    _, _, plain = review
    data = reported(review, cli)
    rows = {r["path"]: r for r in data["rows"]}
    for name in ("a-1", "a-2", "b-3"):
        for path, (added, removed) in counts(plain[name]).items():
            cell = rows[path]["cells"][name]
            assert (cell["added"], cell["removed"]) == (added, removed), (name, path)
    # D1 (a Finding and a Defect check) and three Findings not merged into a Defect.
    assert rows["calc.py"]["cells"]["a-1"] == {"added": 3, "removed": 1, "pins": 4}
    # The files the fewest Trials touched come first, then by path.
    assert [r["path"] for r in data["rows"]] == [
        "config.toml",
        "notes.txt",
        "src/util/helpers.py",
        "uv.lock",
        "calc.py",
    ]


def test_files_the_ignore_globs_match_collapse_into_one_row(review, cli):
    _, folder, _ = review
    data = reported(review, cli)
    assert [r["path"] for r in data["rows"] if r["ignored"]] == ["uv.lock"]
    page = (folder / "diffs.html").read_text()
    assert ">1 ignored file</button>" in page
    hidden = re.search(r"<tbody id='ignored-rows' hidden>(.*?)</tbody>", page, re.S)
    assert hidden and "uv.lock" in hidden.group(1) and "calc.py" not in hidden.group(1)


def test_columns_are_grouped_by_variant_and_empty_for_trials_that_built_nothing(review, cli):
    _, folder, _ = review
    data = reported(review, cli)
    assert data["variants"] == [
        {"name": "a", "trials": ["a-1", "a-2"]},
        {"name": "b", "trials": ["b-1", "b-2", "b-3"]},
    ]
    states = {name: t["state"] for name, t in data["trials"].items()}
    assert states == {
        "a-1": "built",
        "a-2": "built",
        "b-1": "failed",
        "b-2": "changed nothing",
        "b-3": "built",
    }
    assert data["trials"]["b-1"]["files"] == {} == data["trials"]["b-2"]["files"]
    assert not any("b-1" in r["cells"] or "b-2" in r["cells"] for r in data["rows"])
    page = (folder / "diffs.html").read_text()
    # A cell in every file's row, and in the ignored files' row.
    assert page.count("<td class='off' aria-label='failed'></td>") == len(data["rows"]) + 1


def pins_of(data: dict, trial: str, title: str) -> list[dict]:
    return [p for p in data["pins"] if p["trial"] == trial and p["title"] == title]


def test_a_finding_with_a_line_is_pinned_under_that_line(review, cli):
    data = reported(review, cli)
    calc = data["trials"]["a-1"]["files"]["calc.py"]
    (pin,) = pins_of(data, "a-1", "mul adds")
    assert (pin["path"], pin["line"], pin["kind"], pin["judge"]) == ("calc.py", 21, "Finding", "j")
    assert (pin["severity"], pin["category"], pin["defect"]) == ("major", "behaviour", "D1")
    assert calc["lines"][pin["at"]] == ["+", None, 21, "def mul(a, b):"]
    # Several places in one `where`, one a shorter path than the diff's.
    helper = pins_of(data, "a-1", "helper leaks")
    assert [(p["path"], p["line"]) for p in helper] == [
        ("src/util/helpers.py", 2),
        ("calc.py", 15),
    ]
    assert helper[0]["defect"] is None
    # The Defect check answer is pinned too, over the range it names.
    (check,) = [p for p in data["pins"] if p["kind"] == "Defect check"]
    assert (check["judge"], check["line"], check["end"], check["defect"]) == ("k", 21, 22, "D1")
    assert check["detail"] == "mul adds." and check["title"] == "mul does not multiply"


def test_a_pinned_line_far_from_any_change_is_still_shown(review, cli):
    data = reported(review, cli)
    calc = data["trials"]["a-1"]["files"]["calc.py"]
    (far,) = pins_of(data, "a-1", "far away")
    assert far["line"] == 5
    shown = {i for start, end in calc["shown"] for i in range(start, end)}
    assert far["at"] in shown
    assert {far["at"] - 3, far["at"] + 3} <= shown
    # Lines 9 to 11 are more than three lines from every change and every pin.
    assert not {8, 9, 10} & shown and calc["whole"]
    # Line 1 would be hidden alone, so it is shown.
    assert 0 in shown


def test_a_finding_naming_only_a_file_is_pinned_to_the_top_of_it(review, cli):
    data = reported(review, cli)
    (pin,) = pins_of(data, "a-1", "odd name")
    assert (pin["path"], pin["line"], pin["at"]) == ("calc.py", None, None)
    assert pin["id"] in data["trials"]["a-1"]["files"]["calc.py"]["pins"]


def test_a_finding_outside_the_diff_is_listed_with_its_words(review, cli):
    _, folder, _ = review
    data = reported(review, cli)
    unplaced = [data["pins"][i] for i in data["trials"]["a-1"]["unplaced"]]
    assert [(p["title"], p["where"], p["path"]) for p in unplaced] == [
        ("docs missing", "README.md:3", None),
        ("vague", "somewhere in the tests", None),
    ]
    page = (folder / "diffs.html").read_text()
    assert "<h3 id='unplaced-a-1'>a-1</h3>" in page and "Where: README.md:3" in page


def test_each_defect_lists_its_trials_judges_and_places(review, cli):
    data = reported(review, cli)
    (d1,) = data["defects"]
    assert (d1["id"], d1["severity"], d1["category"]) == ("D1", "major", "behaviour")
    by_trial = {t["trial"]: t for t in d1["trials"]}
    assert by_trial["a-1"]["found_by"] == ["j"] and by_trial["a-1"]["confirmed_by"] == ["k"]
    assert by_trial["a-1"]["standing"] == "present"
    assert by_trial["b-3"]["found_by"] == ["j"] and by_trial["b-3"]["denied_by"] == ["k"]
    assert [data["pins"][i]["line"] for i in by_trial["a-1"]["pins"]] == [21, 21]
    assert [data["pins"][i]["line"] for i in by_trial["b-3"]["pins"]] == [22]


def test_diff_text_is_embedded_safely(review, cli):
    _, folder, _ = review
    data = reported(review, cli)
    page = (folder / "diffs.html").read_text()
    assert "<b>bold</b>" not in page and "</script><b>" not in page
    assert page.count("</script>") == 2  # the data and the viewer's script
    helper = data["trials"]["a-1"]["files"]["src/util/helpers.py"]
    assert helper["lines"][1] == ["+", None, 2, "    return '</script><b>bold</b> & <!--'"]


def test_a_trial_without_the_whole_file_diff_falls_back_to_the_plain_one(review, cli):
    data = reported(review, cli)
    a2 = data["trials"]["a-2"]
    assert a2["source"] == "diff.patch"
    calc = a2["files"]["calc.py"]
    assert not calc["whole"] and len(calc["starts"]) == 2
    # Line numbers carry on from each hunk's header.
    second = calc["lines"][calc["starts"][1]]
    assert second[1] == second[2] and second[1] > 10
    assert ["-", 20, None, "# line 20"] in calc["lines"]
    assert data["trials"]["a-1"]["source"] == "diff-full.patch"


def test_the_page_is_self_contained(review, cli):
    _, folder, _ = review
    reported(review, cli)
    page = (folder / "diffs.html").read_text()
    assert not re.search(r"""(src|href)=['"]?(https?:)?//""", page)
    assert "<link" not in page and "@import" not in page
    assert "prefers-color-scheme: dark" in page and "id='theme'" in page


def test_the_report_links_to_the_page(review, cli):
    _, folder, _ = review
    reported(review, cli)
    assert "[diffs.html](diffs.html)" in (folder / "report.md").read_text()
    assert "<a href='diffs.html'>" in (folder / "report.html").read_text()
    assert "<a href='report.html'>" in (folder / "diffs.html").read_text()


# --- `diff` --------------------------------------------------------------------------


def files_of(diff: str) -> list[str]:
    return re.findall(r"^diff --git (\S+) (\S+)$", diff, flags=re.M)


def test_diff_prints_one_trials_files_against_anothers(review, cli):
    settings, _, _ = review
    code, out, err = cli("diff", settings, "a-1", "b-3")
    assert code == 0, err
    assert files_of(out) == [
        (f"a-1/{p}", f"b-3/{p}")
        for p in ("calc.py", "config.toml", "notes.txt", "src/util/helpers.py", "uv.lock")
    ]
    calc = out.split("diff --git a-1/calc.py b-3/calc.py\n")[1].split("diff --git")[0]
    assert calc.startswith("--- a-1/calc.py\n+++ b-3/calc.py\n@@ -12,11 +12,11 @@\n")
    assert "-# line 15, changed\n+# line 15\n" in calc
    assert "-    return a + b\n+    return a * b\n" in calc
    # Only a-1 changed config.toml: b-3 has the base version, from a-1's own diff.
    assert "-name = 'new value'\n+name = 'old value'\n" in out
    # a-1 added the helper; b-3 has no such file.
    assert "--- a-1/src/util/helpers.py\n+++ /dev/null\n" in out


def test_diff_rebuilds_the_base_version_a_trial_left_alone(review, cli):
    settings, _, _ = review
    # b-3 removed notes.txt; a-1 never changed it, so it has the base version, which
    # only b-3's diff holds.
    code, out, _ = cli("diff", settings, "a-1", "b-3", "notes.txt")
    assert code == 0
    assert out == (
        "diff --git a-1/notes.txt b-3/notes.txt\n--- a-1/notes.txt\n+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n-first note\n-second note\n"
    )


def test_diff_of_one_path_takes_a_shorter_path(review, cli):
    settings, _, _ = review
    code, out, _ = cli("diff", settings, "b-3", "a-1", "helpers.py")
    assert code == 0
    assert files_of(out) == [("b-3/src/util/helpers.py", "a-1/src/util/helpers.py")]
    assert "--- /dev/null\n+++ a-1/src/util/helpers.py\n" in out
    code, out, err = cli("diff", settings, "b-3", "a-1", "README.md")
    assert (code, out) == (0, "")
    assert "Neither b-3 nor a-1 changed README.md" in err


def test_diff_of_a_renamed_file_shows_it_missing_from_a_trial_that_left_it(review, cli):
    settings, folder, _ = review
    rename = (
        "diff --git a/old.py b/new.py\nsimilarity index 50%\nrename from old.py\n"
        "rename to new.py\n--- a/old.py\n+++ b/new.py\n@@ -1,2 +1,2 @@\n keep\n-old\n+new\n"
    )
    with (folder / "trials" / "a-1" / "diff-full.patch").open("a") as patch:
        patch.write(rename)
    code, out, err = cli("diff", settings, "a-1", "b-3", "new.py")
    assert code == 0, err
    assert out == (
        "diff --git a-1/new.py b-3/new.py\n--- a-1/new.py\n+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n-keep\n-new\n"
    )


def test_diff_refuses_an_unknown_trial_or_one_without_the_whole_file_diff(review, cli):
    settings, _, _ = review
    code, out, err = cli("diff", settings, "a-1", "zz-9")
    assert (code, out) == (2, "")
    assert "no Trial 'zz-9'" in err and "a-1" in err
    code, out, err = cli("diff", settings, "a-1", "a-2")
    assert (code, out) == (2, "")
    assert "a-2 has no diff-full.patch" in err


def test_an_ignored_file_keeps_only_the_lines_around_its_changes(review, cli):
    _, folder, _ = review
    # A 40 line lockfile with line 20 changed, as a whole-file diff.
    lines = [f" lock {n}" for n in range(1, 41)]
    lines[19:20] = ["-lock 20", "+lock 20, bumped"]
    head = "diff --git a/big.lock b/big.lock\n--- a/big.lock\n+++ b/big.lock\n@@ -1,40 +1,40 @@\n"
    for name in ("diff.patch", "diff-full.patch"):
        with (folder / "trials" / "a-1" / name).open("a") as patch:
            patch.write(head + "\n".join(lines) + "\n")
    files = reported(review, cli)["trials"]["a-1"]["files"]
    lock = files["big.lock"]
    assert (lock["added"], lock["removed"], lock["trimmed"], lock["whole"]) == (1, 1, True, False)
    # Three lines either side of the change, numbered as in the file.
    assert [row[2] for row in lock["lines"] if row[0] != "-"] == list(range(17, 24))
    assert lock["shown"] == [[0, len(lock["lines"])]]
    assert not files["calc.py"]["trimmed"] and files["calc.py"]["whole"]
