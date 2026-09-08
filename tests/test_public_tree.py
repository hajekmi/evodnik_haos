"""Prevent accidental publication of capture files and deployment addresses."""

import ipaddress
import re
from pathlib import Path


def test_public_tree_has_no_captures_or_deployment_addresses():
    root = Path(__file__).resolve().parents[1]
    ignored = {".git", ".venv", ".pytest_cache", ".ruff_cache", "__pycache__", "htmlcov", "dist"}
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if any(part in ignored for part in relative.parts) or not path.is_file():
            continue
        assert path.suffix not in {".pcap", ".pcapng", ".jsonl", ".csv"}, relative
        assert path.name not in {"secrets.yaml", "protocol-analysis.md"}, relative
        if path.suffix not in {".py", ".md", ".json", ".yml", ".yaml", ".toml", ".txt"}:
            continue
        text = path.read_text()
        for candidate in re.findall(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])", text):
            address = ipaddress.ip_address(candidate)
            assert address.is_loopback or address.is_unspecified, relative
        if path.suffix == ".md":
            assert "../docs/decoded" not in text, relative
