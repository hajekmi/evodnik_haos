"""Set public project links before publication; never publish or contact GitHub."""

import argparse
import json
import re
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository", help="Public GitHub repository URL")
    parser.add_argument("--codeowner", help="GitHub username, with or without @")
    args = parser.parse_args()
    url = args.repository.rstrip("/").removesuffix(".git")
    if not re.fullmatch(r"https://github\.com/[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", url):
        parser.error("Expected https://github.com/OWNER/REPOSITORY")
    if args.codeowner and not re.fullmatch(r"@?[A-Za-z0-9-]+", args.codeowner):
        parser.error("Invalid GitHub username")
    path = Path(__file__).resolve().parents[1] / "custom_components/evodnik/manifest.json"
    manifest = json.loads(path.read_text())
    manifest["documentation"] = url
    manifest["issue_tracker"] = f"{url}/issues"
    if args.codeowner:
        manifest["codeowners"] = [f"@{args.codeowner.removeprefix('@')}"]
    path.write_text(json.dumps(manifest, indent=2) + "\n")
    print("Public project links updated locally.")


if __name__ == "__main__":
    main()
