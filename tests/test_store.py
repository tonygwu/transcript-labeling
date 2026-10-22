"""The answers file: every section present, written whole, a bad file refused."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

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


# ------------------------------------------------------------------ version-3 safety
#
#   VERSIONS   a page's saves carry the version it loaded; a save after an outside
#              change the page has not seen is refused, whoever read the file since
#   RESTART    a version from an earlier server process, or another store object, is refused
#   V2         a version-2 answers file comes back byte-identical after a save that changes nothing
#   SYMLINK    a symlinked file is refused
#   OWN        the store's own writes, and other pages' saves, never invalidate a page
#   MID-SAVE   a change during a save is refused and nothing is written
#   TEMPS      unique temp names; leftovers cleared, even with glob characters in the name

import os
import threading as _threading

from transcript_labeling.store import (DatasetStore, EventLog, ReviewStore, StaleWrite, empty_review, fingerprint,
                                       load_review, locked)


def test_write_atomic_uses_a_unique_temp_name_and_cleans_it_up(tmp_path, monkeypatch):
    seen = []
    real_replace = os.replace

    def spy(src, dst):
        seen.append(Path(src).name)
        return real_replace(src, dst)
    monkeypatch.setattr(os, "replace", spy)
    write_atomic(tmp_path / "f.json", "a")
    write_atomic(tmp_path / "f.json", "b")
    assert len(set(seen)) == 2 and all(n.startswith(".f.json.") and n.endswith(".tmp") for n in seen)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["f.json"]


def test_write_atomic_refuses_when_the_file_moved_since_it_was_read(tmp_path):
    p = tmp_path / "f.json"
    p.write_text("one")
    fp = fingerprint(p)
    p.write_text("two, from git pull")
    with pytest.raises(StaleWrite):
        write_atomic(p, "mine", expect=fp)
    assert p.read_text() == "two, from git pull"
    assert sorted(x.name for x in tmp_path.iterdir()) == ["f.json"]
    with pytest.raises(StaleWrite):
        write_atomic(tmp_path / "new.json", "x", expect=fp)  # expected a file that is not there
    write_atomic(tmp_path / "new.json", "x", expect=None)


def test_the_fingerprint_of_a_write_is_the_file_after_the_rename(tmp_path):
    written = []
    p = write_atomic(tmp_path / "f.json", "hello", written=written)
    assert written == [fingerprint(p)]


def answers_store(tmp_path):
    return DatasetStore(tmp_path / "human_answers.json", tmp_path / "state" / ".tl.lock")


def add(label):
    return lambda a: {**a, "labels": {**a["labels"], label: {}}}


def outside_write(p, labels):
    p.write_text(json.dumps({"labels": {k: {} for k in labels}, "attribution": {}}))


def test_a_page_saves_with_the_version_it_loaded(tmp_path):
    store = answers_store(tmp_path)
    _, v = store.load_versioned()
    _, sha, v_after = store.update_hashed(add("a/x"), v)
    assert v_after == v  # its own save does not invalidate the page
    assert sha == hashlib.sha256(store.path.read_bytes()).hexdigest()
    store.update(add("a/y"), v)
    assert set(json.loads(store.path.read_text())["labels"]) == {"a/x", "a/y"}


def test_two_pages_that_loaded_after_the_last_outside_change_both_save(tmp_path):
    store = answers_store(tmp_path)
    _, tab1 = store.load_versioned()
    _, tab2 = store.load_versioned()
    store.update(add("a/one"), tab1)
    store.update(add("a/two"), tab2)  # merges onto the current file
    assert set(json.loads(store.path.read_text())["labels"]) == {"a/one", "a/two"}


def test_an_outside_change_is_refused_even_after_another_page_read_the_file(tmp_path):
    store = answers_store(tmp_path)
    _, stale = store.load_versioned()
    store.update(add("a/x"), stale)
    outside_write(store.path, ["a/reverted"])  # git pull, after the page loaded
    answers, fresh = store.load_versioned()  # another tab, the landing page, or `tl wait` reads it
    assert set(answers["labels"]) == {"a/reverted"} and fresh != stale
    with pytest.raises(StaleWrite, match="reload the page"):
        store.update(add("a/lost"), stale)
    assert set(json.loads(store.path.read_text())["labels"]) == {"a/reverted"}
    store.update(add("a/kept"), fresh)  # the page that saw the change saves
    assert set(json.loads(store.path.read_text())["labels"]) == {"a/reverted", "a/kept"}


def test_an_outside_change_with_no_read_in_between_is_refused(tmp_path):
    store = answers_store(tmp_path)
    _, v = store.load_versioned()
    store.update(add("a/x"), v)
    outside_write(store.path, ["a/reverted"])
    with pytest.raises(StaleWrite):
        store.update(add("a/lost"), v)
    with pytest.raises(StaleWrite):  # still refused: the count does not go back
        store.update(add("a/lost"), v)


def test_a_deleted_file_is_an_outside_change(tmp_path):
    store = answers_store(tmp_path)
    _, v = store.load_versioned()
    store.update(add("a/x"), v)
    store.path.unlink()
    with pytest.raises(StaleWrite):
        store.update(add("a/y"), v)
    assert not store.path.exists()


def test_a_version_from_before_a_restart_is_refused(tmp_path):
    store = answers_store(tmp_path)
    _, old = store.load_versioned()
    store.update(add("a/x"), old)
    restarted = answers_store(tmp_path)  # a restarted server builds new store objects
    with pytest.raises(StaleWrite):
        restarted.update(add("a/y"), old)
    _, new = restarted.load_versioned()
    restarted.update(add("a/y"), new)
    assert set(json.loads(store.path.read_text())["labels"]) == {"a/x", "a/y"}


def test_a_new_store_counts_nothing_on_its_first_read(tmp_path):
    p = tmp_path / "human_answers.json"
    outside_write(p, ["a/pulled"])
    store = answers_store(tmp_path)
    _, v = store.load_versioned()
    store.update(add("a/mine"), v)
    assert set(json.loads(p.read_text())["labels"]) == {"a/pulled", "a/mine"}


def test_a_save_must_carry_a_version(tmp_path):
    store = answers_store(tmp_path)
    for bad in (None, "", 3):
        with pytest.raises(ValueError, match="must carry the version"):
            store.update_hashed(add("a/x"), bad)
    assert not store.path.exists()


def test_leftover_temps_from_a_crashed_save_are_removed_on_the_next_save(tmp_path):
    for n in range(3):
        (tmp_path / f".human_answers.json.{n}.dead.tmp").write_text("partial")
    (tmp_path / ".other.json.1.dead.tmp").write_text("not ours")
    store = answers_store(tmp_path)
    store.update(lambda a: a, store.load_versioned()[1])
    assert sorted(x.name for x in tmp_path.iterdir()) == [".other.json.1.dead.tmp", "human_answers.json", "state"]


def test_temp_cleanup_treats_glob_characters_in_the_name_literally(tmp_path):
    p = tmp_path / "a[1].json"
    (tmp_path / ".a[1].json.9.dead.tmp").write_text("ours")
    (tmp_path / ".a1.json.9.dead.tmp").write_text("not ours, but `a[1]` as a glob matches it")
    store = DatasetStore(p, tmp_path / "state" / ".tl.lock")
    store.update(lambda a: a, store.load_versioned()[1])
    assert sorted(x.name for x in tmp_path.iterdir()) == [".a1.json.9.dead.tmp", "a[1].json", "state"]


def test_dataset_store_refuses_a_save_when_the_file_changes_mid_save(tmp_path):
    store = answers_store(tmp_path)
    _, v = store.load_versioned()
    store.update(add("a/x"), v)

    def change(a):
        outside_write(store.path, ["a/pulled"])  # a revert, mid-save
        return {**a, "labels": {**a["labels"], "a/mine": {}}}
    with pytest.raises(StaleWrite):
        store.update(change, v)
    assert json.loads(store.path.read_text())["labels"] == {"a/pulled": {}}
    assert sorted(x.name for x in tmp_path.iterdir()) == ["human_answers.json", "state"]
    assert set(store.load()) == set(SECTIONS)


def test_the_dataset_lock_serializes_saves(tmp_path):
    lock = tmp_path / "state" / ".tl.lock"
    store = DatasetStore(tmp_path / "a.json", lock)
    _, v = store.load_versioned()
    order = []
    with locked(lock):
        t = _threading.Thread(target=lambda: (store.update(lambda a: a, v), order.append("save")))
        t.start()
        t.join(0.3)
        order.append("released")
    t.join(5)
    assert order == ["released", "save"]


def test_review_store_and_event_log(tmp_path):
    rv = ReviewStore(tmp_path / "review.json", tmp_path / "state" / ".tl.lock")
    review, v = rv.load_versioned()
    assert review == empty_review() and not (tmp_path / "review.json").exists()
    rv.update(lambda r: {**r, "notes": {"a/x": {"text": "check the start"}}}, v)
    assert load_review(tmp_path / "review.json")["notes"]["a/x"]["text"] == "check the start"
    with pytest.raises(ValueError):
        rv.update(lambda r: {"notes": {}}, v)
    (tmp_path / "review.json").write_text('{"schema_version": 2}')
    with pytest.raises(SystemExit, match="not a version-1 review file"):
        rv.load()
    log = EventLog(tmp_path / "state" / "events.jsonl")
    log.append("answer", "a/x", "operator", "browser", "ab" * 32, now="2026-10-22T05:00:00+00:00")
    assert log.read() == [{"time": "2026-10-22T05:00:00+00:00", "kind": "answer", "id": "a/x",
                           "principal": "operator", "login": "browser", "sha256_after": "ab" * 32}]


def test_review_store_refuses_outside_changes_the_page_has_not_seen(tmp_path):
    path = tmp_path / "review.json"
    rv = ReviewStore(path, tmp_path / "state" / ".tl.lock")
    _, v = rv.load_versioned()
    rv.update(lambda r: {**r, "done": {"a/x": {}}}, v)
    path.write_text(json.dumps({**empty_review(), "done": {"a/pulled": {}}}))
    _, fresh = rv.load_versioned()
    with pytest.raises(StaleWrite, match="reload the page"):
        rv.update(lambda r: {**r, "done": {**r["done"], "a/mine": {}}}, v)
    rv.update(lambda r: {**r, "done": {**r["done"], "a/mine": {}}}, fresh)
    assert set(json.loads(path.read_text())["done"]) == {"a/pulled", "a/mine"}


def test_an_outside_write_just_after_our_rename_is_still_caught(tmp_path, monkeypatch):
    store = answers_store(tmp_path)
    _, v = store.load_versioned()
    real_replace = os.replace

    def replace_then_git(src, dst):
        real_replace(src, dst)
        os.replace = real_replace
        outside_write(Path(dst), ["a/reverted"])  # lands before the store could re-read the file
    monkeypatch.setattr(os, "replace", replace_then_git)
    store.update(add("a/x"), v)
    with pytest.raises(StaleWrite):
        store.update(add("a/y"), v)


def test_a_file_that_changes_while_it_is_read_is_refused(tmp_path):
    store = answers_store(tmp_path)
    outside_write(store.path, ["a/one"])
    real_read = store._read

    def read_during_git():
        value = real_read()
        outside_write(store.path, ["a/two"])
        return value
    store._read = read_during_git
    with pytest.raises(StaleWrite, match="changed on disk while it was read"):
        store.load_versioned()


def test_a_second_store_object_on_one_file_refuses_rather_than_accepts(tmp_path):
    first = answers_store(tmp_path)
    _, stale = first.load_versioned()
    first.update(add("a/x"), stale)
    outside_write(first.path, ["a/reverted"])
    second = answers_store(tmp_path)  # a mistake: two objects on one file
    second.load_versioned()  # its first read counts nothing
    with pytest.raises(StaleWrite):
        second.update(add("a/lost"), stale)  # the token tells the objects apart
    assert set(json.loads(first.path.read_text())["labels"]) == {"a/reverted"}


def test_a_version_for_one_file_is_refused_by_the_other(tmp_path):
    answers = answers_store(tmp_path)
    review = ReviewStore(tmp_path / "review.json", tmp_path / "state" / ".tl.lock")
    _, v_answers = answers.load_versioned()
    review.load_versioned()
    with pytest.raises(StaleWrite):
        review.update(lambda r: r, v_answers)


def test_no_version_is_issued_without_a_read():
    public = {n for n in dir(DatasetStore) if not n.startswith("_")}
    assert public == {"load", "load_versioned", "update", "update_hashed"}


#: An answers file exactly as verbatim's dump_answers wrote it: one-space indent, keys in
#: insertion order (not sorted), non-ASCII kept, a final newline.
VERBATIM_FILE = (
    '{\n "labels": {\n  "talk/b": {\n   "present": true,\n   "checked_by": "operator",\n'
    '   "notes": "Zoë\'s"\n  },\n  "talk/a": {\n   "present": false\n  }\n },\n'
    ' "attribution": {\n  "q1": {\n   "answer": "subject"\n  }\n },\n "spans": {},\n "corrections": {}\n}\n')


def test_a_v2_answers_file_comes_back_byte_identical(tmp_path):
    p = tmp_path / "human_answers.json"
    p.write_text(VERBATIM_FILE)
    store = answers_store(tmp_path)
    store.update(lambda a: a, store.load_versioned()[1])
    assert p.read_text() == VERBATIM_FILE


def test_a_two_section_v2_file_gains_empty_spans_and_corrections(tmp_path):
    # verbatim's own loader fills in the two sections an older file lacks
    p = tmp_path / "human_answers.json"
    p.write_text('{"labels": {"talk/a": {}}, "attribution": {}}')
    store = answers_store(tmp_path)
    store.update(lambda a: a, store.load_versioned()[1])
    assert p.read_text() == '{\n "labels": {\n  "talk/a": {}\n },\n "attribution": {},\n "spans": {},\n' \
                            ' "corrections": {}\n}\n'


def test_a_symlinked_answers_file_is_refused(tmp_path):
    real = tmp_path / "real.json"
    outside_write(real, ["a/orig"])
    (tmp_path / "human_answers.json").symlink_to(real)
    store = answers_store(tmp_path)
    with pytest.raises(SystemExit, match="is a symlink"):
        store.load_versioned()
    assert (tmp_path / "human_answers.json").is_symlink()
