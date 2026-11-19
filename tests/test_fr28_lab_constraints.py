"""FR-28 / PRD 6.8: one-page "Lab Constraints and Equivalences" appendix, one table with
evidence IDs; the evidence itself lives in docs/lab-constraints-evidence.md."""

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "lab-constraints.md"
EVIDENCE = ROOT / "docs" / "lab-constraints-evidence.md"
WORD_LIMIT = 500  # PRD 6.8 target; the one-page landscape Letter render is a human check

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
ID_LIST = re.compile(r"^E\d+(, E\d+)*$")


def table_lines(path=DOC):
    return [ln for ln in path.read_text().splitlines() if ln.startswith("|")]


def cells(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def rows(path=DOC):
    lines = table_lines(path)
    return {cells(ln)[0]: cells(ln) for ln in lines[2:]}


def evidence_ids():
    return {r[0]: r for r in rows(EVIDENCE).values()}


def test_exactly_one_table_with_required_columns():
    lines = table_lines()
    assert cells(lines[0]) == COLUMNS
    assert set(cells(lines[1])) == {"---"}
    text = DOC.read_text().splitlines()
    idx = [i for i, ln in enumerate(text) if ln.startswith("|")]
    assert idx == list(range(idx[0], idx[0] + len(idx)))


def test_covers_required_constraints():
    names = list(rows())
    assert [n for n in names if n in REQUIRED_ROWS] == REQUIRED_ROWS


def test_every_cell_filled():
    for name, row in rows().items():
        assert len(row) == len(COLUMNS), name
        for cell in row:
            assert cell and "TBD" not in cell, name


def test_one_sentence_per_cell():
    for name, row in rows().items():
        for cell in row:
            assert not re.search(r"[.!?]\s+\S", cell), (name, cell)


def test_within_word_limit():
    assert len(DOC.read_text().split()) <= WORD_LIMIT


def test_evidence_column_holds_ids_defined_in_evidence_file():
    defined = evidence_ids()
    used = set()
    for name, row in rows().items():
        assert ID_LIST.match(row[3]), (name, row[3])
        used |= set(row[3].split(", "))
    assert used == set(defined), used ^ set(defined)
    assert "lab-constraints-evidence.md" in DOC.read_text()


def test_evidence_is_named_not_invented():
    # Every item names an artifact to attach or capture, or a file in the repo.
    for eid, (_, evidence, source, status) in evidence_ids().items():
        assert evidence and source, eid
        assert status in {"To attach", "To capture", "To run and capture", "In repo"}, eid


def test_records_prd_6_8_network_decisions():
    r = rows()
    assert "HTTP 80 only" in r["No domain or certificate"][2]
    assert "`UseNat=false`" in r["NAT gateway"][2]
    assert "driftgate-storage" in r["Session stops"][2]
    assert "80/443" not in DOC.read_text()


def test_least_privilege_marks_enforced_and_documented():
    status = rows()["Least privilege"][5]
    assert status.startswith("Enforced:") and "documented only:" in status
    enforced, documented = status.split("documented only:")
    for control in (
        "port 80 only",
        "ALB-only serving ingress",
        "no build-host inbound",
        "no SSH",
        "IMDSv2",
        "bucket policy",
        "Lambda resource policies",
        "1,800 s approval tokens",
    ):
        assert control in enforced, control
    assert "identity policies" in documented
    assert "build-host start/stop" in documented


@pytest.mark.skipif(
    not (ROOT / "iam" / "controls.json").exists(), reason="iam/controls.json lands with T-29"
)
def test_status_matches_iam_controls():
    controls = json.loads((ROOT / "iam" / "controls.json").read_text())["controls"]
    identity = [c for c in controls if c["kind"] == "identity-policy"]
    assert identity and all(c["status"] == "documented-only" for c in identity)
    enforced_ids = {c["id"] for c in controls if c["status"] == "enforced"}
    assert {"sg-alb", "sg-serving", "sg-build-host", "ssm-not-ssh", "imdsv2"} <= enforced_ids
    assert {"s3-bucket-policy", "approval-token"} <= enforced_ids
