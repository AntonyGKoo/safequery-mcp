from __future__ import annotations

import json

from safequery_mcp.audit import GENESIS, AuditLog, verify


def _write_three(path):
    log = AuditLog(path)
    log.record(tool="list_tables", decision="allowed")
    log.record(tool="run_query", sql="SELECT 1", decision="allowed", rows=1)
    log.record(tool="run_query", sql="DROP TABLE x", decision="blocked", code="not_a_select")
    return log


def test_entries_form_a_chain(tmp_path):
    path = tmp_path / "audit.jsonl"
    _write_three(path)
    entries = [json.loads(line) for line in path.read_text().splitlines()]
    assert entries[0]["prev"] == GENESIS
    assert entries[1]["prev"] == entries[0]["hash"]
    assert entries[2]["prev"] == entries[1]["hash"]
    assert verify(path).ok and verify(path).entries == 3


def test_editing_an_entry_is_detected(tmp_path):
    path = tmp_path / "audit.jsonl"
    _write_three(path)
    lines = path.read_text().splitlines()
    lines[1] = lines[1].replace("SELECT 1", "SELECT 2")
    path.write_text("\n".join(lines) + "\n")
    result = verify(path)
    assert not result.ok and result.bad_line == 2


def test_deleting_an_entry_is_detected(tmp_path):
    path = tmp_path / "audit.jsonl"
    _write_three(path)
    lines = path.read_text().splitlines()
    path.write_text("\n".join([lines[0], lines[2]]) + "\n")
    result = verify(path)
    assert not result.ok and result.bad_line == 2


def test_chain_continues_across_restarts(tmp_path):
    path = tmp_path / "audit.jsonl"
    _write_three(path)
    AuditLog(path).record(tool="list_tables", decision="allowed")   # a new process
    result = verify(path)
    assert result.ok and result.entries == 4


def test_missing_log_verifies_as_empty(tmp_path):
    result = verify(tmp_path / "nothing.jsonl")
    assert result.ok and result.entries == 0
