"""Merge validated Dependabot updates without executing pull request code."""

import base64
import json
import os
import re
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

REQUIRED_JOBS = {"tests", "hassfest", "hacs"}
ACTION_PIN = re.compile(r"^(\s*- uses: [\w.-]+/[\w./-]+)@[0-9a-f]{40}(?:\s+#.*)?$", re.MULTILINE)
PYTHON_PIN = re.compile(r"^([\w.-]+)==[0-9][\w.!+\-]*$", re.MULTILINE)


class GitHub:
    """Use the workflow token only with the GitHub repository API."""

    def __init__(self, repository: str, token: str) -> None:
        self.repository = repository
        self.token = token

    def __call__(self, method: str, path: str, body: dict | None = None):
        request = Request(
            f"https://api.github.com/repos/{self.repository}{path}",
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "User-Agent": "evodnik-dependency-maintenance",
            },
            method=method,
        )
        with urlopen(request, timeout=30) as response:
            content = response.read()
        return json.loads(content) if content else None


def only_dependency_versions(path: str, before: str, after: str) -> bool:
    """Allow existing action SHA pins or pinned development package versions."""
    if path.startswith(".github/workflows/") and path.endswith((".yml", ".yaml")):
        pattern = ACTION_PIN
    elif path == "requirements-dev.txt":
        pattern = PYTHON_PIN
    else:
        return False
    return (
        before != after
        and 0 < len(pattern.findall(before)) == len(pattern.findall(after))
        and pattern.sub(r"\1@VERSION", before) == pattern.sub(r"\1@VERSION", after)
    )


def file_text(api, path: str, ref: str) -> str:
    """Read a bounded text file as data, never as executable code."""
    content = api("GET", f"/contents/{quote(path)}?{urlencode({'ref': ref})}")
    if content.get("encoding") != "base64" or content.get("size", 0) > 131072:
        raise ValueError("Dependency file is too large or is not ordinary file content.")
    return base64.b64decode(content["content"], validate=False).decode("utf-8")


def maintain(api, run_id: int) -> bool:
    """Merge one current, successful Dependabot PR; otherwise leave it open."""
    run = api("GET", f"/actions/runs/{run_id}")
    if (
        run.get("event") != "pull_request"
        or run.get("conclusion") != "success"
        or run.get("path") != ".github/workflows/validate.yml"
        or len(run.get("pull_requests", [])) != 1
    ):
        return False
    association = run["pull_requests"][0]
    number = association["number"]
    pr = api("GET", f"/pulls/{number}")
    if (
        pr["user"]["login"] != "dependabot[bot]"
        or pr["user"]["type"] != "Bot"
        or pr["state"] != "open"
        or pr["draft"]
        or pr["head"]["repo"]["full_name"] != api.repository
        or not pr["head"]["ref"].startswith("dependabot/")
        or pr["head"]["sha"] != run["head_sha"]
        or pr["base"]["ref"] != "main"
        or pr["base"]["sha"] != association["base"]["sha"]
        or pr["mergeable_state"] != "clean"
    ):
        return False
    jobs = api(
        "GET",
        f"/actions/runs/{run_id}/attempts/{run['run_attempt']}/jobs?per_page=100",
    )["jobs"]
    if any(
        len(matches := [job for job in jobs if job["name"] == name]) != 1
        or matches[0]["conclusion"] != "success"
        for name in REQUIRED_JOBS
    ):
        return False
    if not 1 <= pr["changed_files"] <= 20:
        return False
    files = api("GET", f"/pulls/{number}/files?per_page=100")
    if len(files) != pr["changed_files"]:
        return False
    for file in files:
        if file["status"] != "modified":
            return False
        path = file["filename"]
        before = file_text(api, path, pr["base"]["sha"])
        after = file_text(api, path, pr["head"]["sha"])
        if not only_dependency_versions(path, before, after):
            return False
    current = api("GET", f"/pulls/{number}")
    if (
        current["state"] != "open"
        or current["head"]["sha"] != pr["head"]["sha"]
        or current["base"]["sha"] != pr["base"]["sha"]
        or current["mergeable_state"] != "clean"
    ):
        return False
    result = api(
        "PUT",
        f"/pulls/{number}/merge",
        {"sha": pr["head"]["sha"], "merge_method": "merge"},
    )
    if not result.get("merged"):
        raise RuntimeError("GitHub did not merge the validated pull request.")
    print(f"Merged Dependabot pull request #{number}.")
    branch = quote(pr["head"]["ref"], safe="/")
    try:
        reference = api("GET", f"/git/ref/heads/{branch}")
    except HTTPError as error:
        if error.code != 404:
            raise
        return True
    if reference["object"]["sha"] == pr["head"]["sha"]:
        api("DELETE", f"/git/refs/heads/{branch}")
    return True


if __name__ == "__main__":
    github = GitHub(os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"])
    if not maintain(github, int(os.environ["VALIDATION_RUN_ID"])):
        print("No eligible Dependabot update; pull requests were left unchanged.")
