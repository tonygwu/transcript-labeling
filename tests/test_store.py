"""The answers file: every section present, written whole, a bad file refused."""
from __future__ import annotations

import json

import pytest

from transcript_labeling.store import SECTIONS, FileStore, dump_answers, load_answers, write_atomic


def test_missing_file_reads_as_every_section_empty(tmp_path):
    assert load_answers(tmp_path / "none.json") == {s: {} for s in SECTIONS}


@pytest.mark.parametrize("raw", [[], {"labels": {}}, {"labels": {}, "attribution": {}, "extra": {}},
                                 {"labels": [], "attribution": {}}])
def test_a_file_that_is_not_an_answers_file_is_refused(tmp_path, raw):
    p = tmp_path / "a.json"
    p.write_text(json.dumps(raw))
    with pytest.raises(SystemExit, match="not an answers file"):
        load_answers(p)


def test_a_two_section_file_reads_with_all_four(tmp_path):
    p = tmp_path / "a.json"
    p.write_text(json.dumps({"labels": {"k": {}}, "attribution": {}}))
    assert load_answers(p) == {"labels": {"k": {}}, "attribution": {}, "spans": {}, "corrections": {}}


def test_dump_format_is_fixed():
    assert dump_answers({"labels": {"é": 1}}) == '{\n "labels": {\n  "é": 1\n }\n}\n'


def test_filestore_update_persists_and_a_raising_change_writes_nothing(tmp_path):
    p = tmp_path / "human_answers.json"
    store = FileStore(p)
    out = store.update(lambda a: {**a, "labels": {"p/a": {"x": 1}}})
    assert out["labels"] == {"p/a": {"x": 1}}
    assert json.loads(p.read_text())["labels"] == {"p/a": {"x": 1}}
    before = p.read_bytes()

    def boom(a):
        raise ValueError("no")
    with pytest.raises(ValueError):
        store.update(boom)
    with pytest.raises(ValueError, match="exactly the sections"):
        store.update(lambda a: {"labels": {}})
    assert p.read_bytes() == before
    assert store.load()["labels"] == {"p/a": {"x": 1}}


def test_write_atomic_leaves_no_temp_file(tmp_path):
    p = write_atomic(tmp_path / "d" / "f.txt", "hi")
    assert p.read_text() == "hi"
    assert sorted(x.name for x in p.parent.iterdir()) == ["f.txt"]
