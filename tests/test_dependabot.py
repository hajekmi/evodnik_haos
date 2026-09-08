"""Keep dependency maintenance limited to successful, unchanged bot updates."""

import base64
from copy import deepcopy
from urllib.error import HTTPError

import pytest

from tools.merge_dependabot import maintain, only_dependency_versions

OLD_PIN = "a" * 40
NEW_PIN = "b" * 40
BEFORE = f"steps:\n  - uses: actions/checkout@{OLD_PIN} # v6\n"
AFTER = f"steps:\n  - uses: actions/checkout@{NEW_PIN} # v7\n"


@pytest.mark.parametrize(
    ("path", "before", "after", "allowed"),
    [
        (".github/workflows/validate.yml", BEFORE, AFTER, True),
        ("requirements-dev.txt", "ruff==0.15.0\n", "ruff==0.16.0\n", True),
        (".github/workflows/validate.yml", BEFORE, BEFORE, False),
        (".github/workflows/validate.yml", BEFORE, AFTER + "  - run: echo unsafe\n", False),
        (".github/workflows/validate.yml", BEFORE, AFTER.replace("actions/", "other/"), False),
        (".github/workflows/validate.yml", BEFORE, AFTER.replace(NEW_PIN, "main"), False),
        ("requirements-dev.txt", "ruff==0.15.0\n", "other==0.16.0\n", False),
        ("requirements-dev.txt", "ruff==0.15.0\n", "ruff @ https://example.org/pkg\n", False),
        ("tools/merge_dependabot.py", BEFORE, AFTER, False),
        ("custom_components/evodnik/proxy.py", BEFORE, AFTER, False),
    ],
)
def test_dependency_update_scope(path, before, after, allowed):
    assert only_dependency_versions(path, before, after) is allowed


class FakeGitHub:
    """Record mutation attempts without network access or a GitHub token."""

    repository = "example/project"

    def __init__(self):
        self.run = {
            "event": "pull_request",
            "conclusion": "success",
            "path": ".github/workflows/validate.yml",
            "pull_requests": [{"number": 1, "base": {"sha": "base"}}],
            "head_sha": "head",
            "run_attempt": 1,
        }
        self.pr = {
            "user": {"login": "dependabot[bot]", "type": "Bot"},
            "state": "open",
            "draft": False,
            "head": {
                "repo": {"full_name": self.repository},
                "ref": "dependabot/github_actions/actions/checkout-7",
                "sha": "head",
            },
            "base": {"ref": "main", "sha": "base"},
            "mergeable_state": "clean",
            "changed_files": 1,
        }
        self.jobs = [
            {"name": name, "conclusion": "success"} for name in ("tests", "hacs", "hassfest")
        ]
        self.files = [{"filename": ".github/workflows/validate.yml", "status": "modified"}]
        self.after = AFTER
        self.refresh = None
        self.pr_reads = 0
        self.branch_sha = "head"
        self.mutations = []

    def __call__(self, method, path, body=None):
        if method != "GET":
            self.mutations.append((method, path, body))
            return {"merged": True}
        if path == "/actions/runs/1":
            return deepcopy(self.run)
        if path == "/pulls/1":
            self.pr_reads += 1
            return deepcopy(self.refresh if self.pr_reads > 1 and self.refresh else self.pr)
        if path.endswith("/jobs?per_page=100"):
            return {"jobs": deepcopy(self.jobs)}
        if path.endswith("/files?per_page=100"):
            return deepcopy(self.files)
        if path.startswith("/contents/"):
            text = BEFORE if path.endswith("ref=base") else self.after
            return {
                "encoding": "base64",
                "size": len(text),
                "content": base64.b64encode(text.encode()).decode(),
            }
        if path.startswith("/git/ref/"):
            if self.branch_sha is None:
                raise HTTPError("https://api.github.com", 404, "Not Found", {}, None)
            return {"object": {"sha": self.branch_sha}}
        raise AssertionError((method, path, body))


def test_validated_bot_update_merges_matching_head_and_deletes_its_branch():
    api = FakeGitHub()
    assert maintain(api, 1)
    assert api.mutations == [
        ("PUT", "/pulls/1/merge", {"sha": "head", "merge_method": "merge"}),
        ("DELETE", "/git/refs/heads/dependabot/github_actions/actions/checkout-7", None),
    ]


@pytest.mark.parametrize(
    "reason",
    [
        "failed_run",
        "wrong_workflow",
        "human",
        "fork",
        "stale_head",
        "stale_base",
        "conflict",
        "failed_job",
        "missing_job",
        "code_change",
        "new_file",
        "changed_during_review",
    ],
)
def test_ineligible_updates_never_mutate_github(reason):
    api = FakeGitHub()
    if reason == "failed_run":
        api.run["conclusion"] = "failure"
    elif reason == "wrong_workflow":
        api.run["path"] = ".github/workflows/other.yml"
    elif reason == "human":
        api.pr["user"] = {"login": "contributor", "type": "User"}
    elif reason == "fork":
        api.pr["head"]["repo"]["full_name"] = "contributor/project"
    elif reason == "stale_head":
        api.pr["head"]["sha"] = "new-head"
    elif reason == "stale_base":
        api.pr["base"]["sha"] = "new-base"
    elif reason == "conflict":
        api.pr["mergeable_state"] = "dirty"
    elif reason == "failed_job":
        api.jobs[0]["conclusion"] = "failure"
    elif reason == "missing_job":
        api.jobs.pop()
    elif reason == "code_change":
        api.after += "  - run: echo unexpected\n"
    elif reason == "new_file":
        api.files[0]["status"] = "added"
    elif reason == "changed_during_review":
        api.refresh = deepcopy(api.pr)
        api.refresh["head"]["sha"] = "new-head"
    assert not maintain(api, 1)
    assert api.mutations == []


def test_branch_changed_after_merge_is_not_deleted():
    api = FakeGitHub()
    api.branch_sha = "new-head"
    assert maintain(api, 1)
    assert len(api.mutations) == 1


def test_branch_already_deleted_by_github_is_successful():
    api = FakeGitHub()
    api.branch_sha = None
    assert maintain(api, 1)
    assert len(api.mutations) == 1
