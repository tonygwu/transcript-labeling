"""Version-2 datasets: the manifest is checked whole, and a rebuild cannot strand answers."""
from __future__ import annotations

import copy
import json

import pytest

from conftest import KEY, QID
from transcript_labeling.dataset import MODE_SECTION, Session, guard_answers, manifest_errors, write_manifest
from transcript_labeling.page.data import page_data, page_ids, page_payload

GOOD = {"schema_version": 2, "tool": "annotations", "name": "t", "projects": ["demo"], "modes": ["speakers"],
        "blind": True, "transcript_dir": "transcripts",
        "files": {"page": "p.html", "answers": "a.json", "quote_key": "k.json"},
        "recordings": {"a/b": {"projects": ["demo"], "text_sha256": "x", "token_count": 1}}}


def test_a_sound_manifest_has_no_errors():
    assert manifest_errors(GOOD) == []


@pytest.mark.parametrize("change,expect", [
    (lambda m: m.pop("tool"), "missing `tool`"),
    (lambda m: m.update(schema_version=3), "is not 2"),
    (lambda m: m.update(projects=[]), "non-empty list"),
    (lambda m: m.update(projects=["demo", ""]), "non-empty list"),
    (lambda m: m.update(modes=["painting"]), "subset of"),
    (lambda m: m["files"].pop("quote_key"), "files has no `quote_key`"),
    (lambda m: m["recordings"]["a/b"].update(projects=["other"]), "outside the session's"),
])
def test_each_manifest_fault_is_named(change, expect):
    m = copy.deepcopy(GOOD)
    change(m)
    assert any(expect in e for e in manifest_errors(m)), manifest_errors(m)


def test_a_version_2_manifest_that_lost_a_key_names_it_and_does_not_say_migrate():
    m = copy.deepcopy(GOOD)
    del m["tool"]
    errs = manifest_errors(m)
    assert errs == ["missing `tool`"] and not any("migrate" in e for e in errs)


def test_a_version_1_manifest_says_to_migrate_and_not_missing():
    errs = manifest_errors({"schema_version": 1, "study": "x"})
    assert len(errs) == 1 and "migrate" in errs[0] and "missing" not in errs[0]
    assert manifest_errors([]) == ["the manifest is not an object"]


def test_write_manifest_refuses_a_bad_one_and_open_refuses_a_missing_one(tmp_path):
    with pytest.raises(SystemExit, match="REFUSING to write"):
        write_manifest(tmp_path, {**GOOD, "modes": []})
    assert not (tmp_path / "manifest.json").exists()
    with pytest.raises(SystemExit, match="has no manifest.json"):
        Session.open(tmp_path)


def test_session_properties_and_sections(quote_session):
    s = Session.open(quote_session.root)
    assert (s.name, s.projects, s.modes) == ("s", ["demo"], ["speakers", "captions"])
    assert s.sections() == {"spans", "corrections"}
    assert s.page.name == "page.html" and s.answers.name == "human_answers.json"
    assert s.sidecar(KEY).exists()
    assert s.data()["transcript_index"][KEY]["token_count"] == 33
    assert MODE_SECTION == {"checklist": "labels", "speakers": "spans", "captions": "corrections"}


def test_page_data_round_trips_and_escapes_script_end():
    data = {"rows": [{"key": "a/b", "title": "x </script> y"}]}
    payload = page_payload(data)
    assert "</script>" not in payload
    assert page_data("<script>\nconst D = " + payload + ";\n</script>") == data
    with pytest.raises(SystemExit, match="no `const D"):
        page_data("<html></html>")


def test_page_ids_per_section_and_an_empty_page_is_refused():
    ids = page_ids({"rows": [{"key": "a/b"}], "quote_rows": [{"key": "a/b", "quotes": [{"qid": "q"}]}],
                    "transcript_index": {"a/b": {}}})
    assert ids == {"labels": {"a/b"}, "attribution": {"q"}, "spans": {"a/b"}, "corrections": {"a/b"}}
    with pytest.raises(SystemExit, match="carries no recordings"):
        page_ids({})


def guard_args(session, **change):
    data = session.data()
    sidecars = {KEY: json.loads(session.sidecar(KEY).read_text())}
    args = dict(answers_path=session.answers, rows=data["rows"], quote_rows=copy.deepcopy(data["quote_rows"]),
                transcript_index=copy.deepcopy(data["transcript_index"]), allow_off_page=False, sidecars=sidecars,
                old_page=session.page)
    args.update(change)
    return args


def test_guard_passes_an_unchanged_build(quote_session):
    guard_answers(**guard_args(quote_session))


def test_guard_refuses_a_build_that_drops_an_answered_quote_unless_allowed(quote_session):
    args = guard_args(quote_session, quote_rows=[])
    with pytest.raises(SystemExit, match="would not carry answers already saved"):
        guard_answers(**args)
    guard_answers(**{**args, "allow_off_page": True})


def test_guard_refuses_a_changed_transcript_even_when_off_page_is_allowed(quote_session):
    index = {KEY: {"text_sha256": "0" * 64, "token_count": 26}}
    with pytest.raises(SystemExit, match="transcript text changed"):
        guard_answers(**guard_args(quote_session, transcript_index=index, allow_off_page=True))


def test_guard_refuses_an_answered_quote_that_now_covers_other_words(quote_session):
    args = guard_args(quote_session)
    args["quote_rows"][0]["quotes"][0]["quote_end"] = 12
    with pytest.raises(SystemExit, match="would cover other words"):
        guard_answers(**args)


def test_guard_without_an_old_page_refuses_a_result_the_new_page_would_change(quote_session):
    answers = json.loads(quote_session.answers.read_text())
    answers["attribution"][QID]["answer"] = "other"
    quote_session.answers.write_text(json.dumps(answers))
    with pytest.raises(SystemExit, match="change their result"):
        guard_answers(**guard_args(quote_session, old_page=quote_session.root / "gone.html"))


def test_guard_with_no_answers_file_passes(tmp_path, quote_session):
    guard_answers(**guard_args(quote_session, answers_path=tmp_path / "none.json", quote_rows=[]))


@pytest.mark.parametrize("change,expect", [
    (lambda m: m.update(projects=[["a"]]), "non-empty list of names"),
    (lambda m: m.update(files=None), "files must be an object"),
    (lambda m: m.update(recordings={"a/b": None}), "recordings must be an object of objects"),
    (lambda m: m.update(modes=[["a"]]), "subset of"),
    (lambda m: m.update(modes=5), "subset of"),
    (lambda m: m["recordings"]["a/b"].update(projects=[["x"]]), "outside the session's"),
])
def test_malformed_parts_are_named_never_a_crash(change, expect):
    m = copy.deepcopy(GOOD)
    change(m)
    assert any(expect in e for e in manifest_errors(m)), manifest_errors(m)


@pytest.mark.parametrize("name", ["my set", "a/b", "-lead", "", "x?y"])
def test_a_session_name_the_server_could_not_reach_is_refused(name):
    assert any("must match" in e for e in manifest_errors({**GOOD, "name": name}))
