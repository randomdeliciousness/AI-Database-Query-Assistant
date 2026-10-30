"""The dev report markdown cites the exact bytes of the committed tune outputs."""

import hashlib
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
REPORT_MD = REPO / "eval" / "retrieval_report_dev.md"


def _cited_sha256(text: str, name: str) -> str:
    pattern = rf"`eval/{re.escape(name)}`\s*\(sha256\s*`([0-9a-f]{{64}})`\)"
    matches = re.findall(pattern, text)
    assert len(matches) == 1, f"expected one sha256 citation for {name}"
    return matches[0]


@pytest.mark.parametrize("name", ["retrieval_report_dev.json", "retrieval_params.json"])
def test_dev_report_cites_committed_bytes(name):
    cited = _cited_sha256(REPORT_MD.read_text(encoding="utf-8"), name)
    actual = hashlib.sha256((REPO / "eval" / name).read_bytes()).hexdigest()
    assert cited == actual, f"{name}: cited {cited}, file is {actual}"
