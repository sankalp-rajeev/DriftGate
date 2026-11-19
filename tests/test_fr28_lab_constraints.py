"""FR-28: one-page "Lab Constraints and Equivalences" appendix, one table."""

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "lab-constraints.md"

COLUMNS = [
    "Constraint",
    "What the Lab does",
    "Equivalent used",
    "Evidence",
    "What is lost",
    "Enforced or documented",
]
REQUIRED_ROWS = [
    "CodePipeline",
    "CodeBuild",
    "IAM role creation",
    "Least privilege",
    "Research layer on laptop",
    "Cost Explorer lag",
    "Session stops",
]


def table_lines():
    return [ln for ln in DOC.read_text().splitlines() if ln.startswith("|")]


def cells(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def rows():
    lines = table_lines()
    return {cells(ln)[0]: cells(ln) for ln in lines[2:]}


def test_exactly_one_table_with_required_columns():
    lines = table_lines()
    assert cells(lines[0]) == COLUMNS
    assert set(cells(lines[1])) == {"---"}
    # one contiguous table
    text = DOC.read_text().splitlines()
    idx = [i for i, ln in enumerate(text) if ln.startswith("|")]
    assert idx == list(range(idx[0], idx[0] + len(idx)))


def test_covers_required_constraints():
    names = list(rows())
    assert [n for n in names if n in REQUIRED_ROWS] == REQUIRED_ROWS


def test_records_prd_6_8_network_decisions():
    r = rows()
    assert "HTTP 80 only" in r["No domain or certificate"][2]
    assert "`UseNat=false`" in r["NAT gateway"][2]
    assert "driftgate-storage" in r["Session stops"][2]
    assert "80/443" not in DOC.read_text()


def test_every_cell_filled():
    for name, row in rows().items():
        assert len(row) == len(COLUMNS), name
        for cell in row:
            assert cell and "TBD" not in cell, name


def test_evidence_is_named_not_invented():
    # Each evidence cell names a probe, screenshot, log or file to attach or capture.
    for name, row in rows().items():
        evidence = row[3].lower()
        assert any(w in evidence for w in ("screenshot", "log", "probe", "output", "iam/")), name


def test_fits_on_one_page():
    assert len(DOC.read_text().split()) <= 1000


def test_least_privilege_marks_enforced_and_documented():
    status = rows()["Least privilege"][5]
    assert "Enforced:" in status and "Documented only:" in status
    for control in (
        "IMDSv2",
        "SSM instead of SSH",
        "S3 bucket policy",
        "Lambda resource policies",
        "1,800 s",
    ):
        assert control in status


@pytest.mark.skipif(
    not (ROOT / "iam" / "controls.json").exists(), reason="iam/controls.json lands with T-29"
)
def test_status_matches_iam_controls():
    controls = json.loads((ROOT / "iam" / "controls.json").read_text())["controls"]
    status = rows()["Least privilege"][5]
    enforced, documented = status.split("**Documented only:**")
    for c in controls:
        if c["kind"] == "identity-policy":
            assert c["status"] == "documented-only"
            assert c["service"].split()[-1].lower() in documented.lower(), c["id"]
