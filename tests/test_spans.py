"""Speaker-range persistence: drafts stay separate, stale or invalid offsets cannot save.

Ported from verbatim-index `scripts/test_annotations_spans.py`. The checks that
need a running server will live in `test_serve.py` with the server port.
"""
from __future__ import annotations

import copy
import hashlib
import json

import pytest

from transcript_labeling import spans
from transcript_labeling.spans import build_sidecar, import_spans, sidecar_path, validate_annotation

RAW = '[00:00:05] Intro from our host here >> I disagree with that point >> Another reply from the host'
SUGGESTIONS = [{"start": 7, "end": 12, "speaker": "subject"}, {"start": 1, "end": 6, "speaker": "other"}]


@pytest.fixture
def transcript():
    return build_sidecar('p/a', RAW, SUGGESTIONS)


@pytest.fixture
def value(transcript):
    return {'key': 'p/a', 'text_sha256': transcript['text_sha256'],
            'token_count': transcript['token_count'], 'checked_by': 'operator',
            'ranges': [{'start': 7, 'end': 12, 'speaker': 'subject', 'origin': 'human'},
                       {'start': 13, 'end': 18, 'speaker': 'other', 'origin': 'model_confirmed'}]}


def test_sidecar_holds_tokens_times_and_suggestions_never_ranges(transcript):
    assert list(transcript) == ["schema_version", "key", "text_sha256", "suggestions_sha256", "token_count",
                                "tokens", "times", "suggestions", "unlocated_or_ambiguous", "suggestion_scope"]
    assert transcript["token_count"] == len(transcript["tokens"]) == 18
    assert transcript["times"] == [[0, 5]]
    assert transcript["suggestions"] == SUGGESTIONS
    assert 'ranges' not in transcript


#: A sidecar verbatim-index's own `build_transcript` built at d5a3a8bf from this text
#: (double spaces and newlines on purpose) and a grade that yields these suggestions.
GOLDEN_RAW = "[00:00:05] Intro  from our host\nhere >> I disagree with that point\n\n>> Another reply from the host here"
GOLDEN_SUGGESTIONS = [
    {"start": 7, "end": 12, "speaker": "subject", "judge": "j1", "mode": "blinded", "dimension": "d1",
     "original_speaker": "subject", "grade_file": "talk-1__judge__blinded__r0.json",
     "grade_sha256": "af226461558f96e9ea9dc1f7675b8b4301f20ec623e5c06d6294bca8e121efa3", "matched_piece": False},
    {"start": 1, "end": 6, "speaker": "other", "judge": "j1", "mode": "blinded", "dimension": "d1",
     "original_speaker": "interlocutor", "grade_file": "talk-1__judge__blinded__r0.json",
     "grade_sha256": "af226461558f96e9ea9dc1f7675b8b4301f20ec623e5c06d6294bca8e121efa3", "matched_piece": False}]
GOLDEN_SHA = "2c4992e155d4fcd55d2763e7552023a782e91b2ea73de519497ed48cbc762ae2"


def test_build_sidecar_is_byte_identical_to_verbatims_builder():
    side = build_sidecar("jane-roe/talk-1", GOLDEN_RAW, GOLDEN_SUGGESTIONS, unlocated=1)
    assert hashlib.sha256(json.dumps(side, ensure_ascii=False).encode()).hexdigest() == GOLDEN_SHA


@pytest.mark.parametrize("mutation", ['hash', 'overlap', 'past_end', 'negative', 'empty', 'float', 'bool', 'draft',
                                      'key', 'no_checker', 'not_list'])
def test_hash_and_ranges_fail_closed(transcript, value, mutation):
    validate_annotation(value, transcript)
    value = copy.deepcopy(value)
    if mutation == 'hash': value['text_sha256'] = 'old'
    elif mutation == 'overlap': value['ranges'][1]['start'] = 10
    elif mutation == 'past_end': value['ranges'][-1]['end'] = 999
    elif mutation == 'negative': value['ranges'][0]['start'] = -1
    elif mutation == 'empty': value['ranges'][0]['end'] = 7
    elif mutation == 'float': value['ranges'][0]['start'] = 7.0
    elif mutation == 'bool': value['ranges'][0]['start'] = True
    elif mutation == 'key': value['key'] = 'p/b'
    elif mutation == 'no_checker': value['checked_by'] = ''
    elif mutation == 'not_list': value['ranges'] = {}
    else: value['ranges'][0]['origin'] = 'model_draft'
    with pytest.raises(ValueError):
        validate_annotation(value, transcript)


@pytest.mark.parametrize("key", ['../secret', 'p/../../secret', '/tmp/x', 'p/a.json', 'p', 'a/b/c'])
def test_path_cannot_escape_sidecars(tmp_path, key):
    with pytest.raises(ValueError):
        sidecar_path(tmp_path / 'page.html', key)


def test_sidecar_path_sits_beside_the_page(tmp_path):
    assert sidecar_path(tmp_path / 'speaker_check.html', 'jane-roe/talk_1') == \
        tmp_path / 'speaker_check.transcripts' / 'jane-roe' / 'talk_1.json'


def test_unreviewed_quote_is_incomplete_not_unclear(transcript, value):
    quote = {'quote_start': 7, 'quote_end': 12}
    value['ranges'] = [{'start': 1, 'end': 6, 'speaker': 'other', 'origin': 'human'}]
    review = spans.quote_review(quote, transcript, value)
    assert (review['answer'], review['review_status'], review['unreviewed_words']) == (None, 'incomplete', 5)
    value['ranges'] = [{'start': 7, 'end': 12, 'speaker': 'unclear', 'origin': 'human'}]
    review = spans.quote_review(quote, transcript, value)
    assert (review['answer'], review['review_status']) == ('unclear', 'complete')


def test_two_speakers_in_a_quote_give_both(transcript, value):
    value['ranges'] = [{'start': 7, 'end': 9, 'speaker': 'subject', 'origin': 'human'},
                       {'start': 9, 'end': 12, 'speaker': 'other', 'origin': 'human'}]
    assert spans.quote_review({'quote_start': 7, 'quote_end': 12}, transcript, value)['answer'] == 'both'


def test_review_ignores_gaps_and_surrounding_speakers():
    transcript = build_sidecar('p/a', '[00:01:00] Hello [ __ ] there >> outside', [])
    value = {'key': 'p/a', 'text_sha256': transcript['text_sha256'], 'token_count': transcript['token_count'],
             'checked_by': 'operator', 'ranges': [
                 {'start': 1, 'end': 2, 'speaker': 'other', 'origin': 'human'},
                 {'start': 5, 'end': 6, 'speaker': 'other', 'origin': 'human'},
                 {'start': 7, 'end': 8, 'speaker': 'subject', 'origin': 'human'}]}
    review = spans.quote_review({'quote_start': 0, 'quote_end': 7}, transcript, value)
    assert (review['answer'], review['total_words'], review['unreviewed_words']) == ('other', 2, 0)


def test_quote_outside_the_transcript_is_refused(transcript, value):
    with pytest.raises(ValueError):
        spans.quote_review({'quote_start': 10, 'quote_end': 99}, transcript, value)


def write_item(page, transcript):
    side = sidecar_path(page, transcript['key'])
    side.parent.mkdir(parents=True, exist_ok=True)
    side.write_text(json.dumps(transcript))


def test_reconcile_derives_results_keeps_legacy_answer_and_marks_stale(tmp_path, transcript, value):
    page = tmp_path / 'page.html'
    write_item(page, transcript)
    rows = [{'key': 'p/a', 'quotes': [{'qid': 'q1', 'quote_start': 7, 'quote_end': 12}, {'qid': 'nopos'}]}]
    answers = {'labels': {}, 'attribution': {'q1': {'qid': 'q1', 'answer': 'other'}}, 'spans': {'p/a': value}}
    out = spans.reconcile_quote_reviews(answers, page, rows)
    assert out['attribution']['q1']['answer'] == 'subject'
    assert out['attribution']['q1']['legacy_answer'] == 'other'
    assert out['attribution']['q1']['review_method'] == 'range_review_v2'
    assert 'nopos' not in out['attribution']
    assert answers['attribution']['q1'] == {'qid': 'q1', 'answer': 'other'}, "reconcile mutated its input"
    again = spans.reconcile_quote_reviews(out, page, rows)
    assert again == out, "a second read changed the result"
    stale = spans.reconcile_quote_reviews({**answers, 'spans': {'p/a': {**value, 'text_sha256': 'x'}}}, page, rows)
    assert (stale['attribution']['q1']['answer'], stale['attribution']['q1']['review_status']) == (None, 'stale')


def test_import_spans_adds_text_and_validates_the_whole_batch_first(tmp_path, transcript, value):
    page = tmp_path / 'page.html'
    write_item(page, transcript)
    answers = tmp_path / 'human_answers.json'
    answers.write_text(json.dumps({'labels': {}, 'attribution': {}, 'spans': {'p/a': value}}))
    result = import_spans(answers, page, tmp_path / 'imported.json')
    assert result['recordings']['p/a']['ranges'][0]['text'] == 'I disagree with that point'
    assert result['recordings']['p/a']['ranges'][1]['speech_text'] == 'Another reply from the host'
    prior = (tmp_path / 'imported.json').read_bytes()
    answers.write_text(json.dumps({'spans': {'p/a': {**value, 'text_sha256': 'bad'}}}))
    with pytest.raises(ValueError):
        import_spans(answers, page, tmp_path / 'imported.json')
    assert (tmp_path / 'imported.json').read_bytes() == prior


def test_import_spans_writes_nothing_when_a_later_item_is_invalid(tmp_path, transcript, value):
    page = tmp_path / 'page.html'
    write_item(page, transcript)
    write_item(page, {**transcript, 'key': 'p/b'})
    answers = tmp_path / 'human_answers.json'
    answers.write_text(json.dumps({'spans': {'p/a': value, 'p/b': {**value, 'key': 'p/b', 'token_count': 1}}}))
    with pytest.raises(ValueError):
        import_spans(answers, page, tmp_path / 'imported.json')
    assert not (tmp_path / 'imported.json').exists()


def test_reconcile_keeps_a_range_reviews_empty_legacy_answer(tmp_path, transcript, value):
    # An entry already derived from ranges carries legacy_answer None; its own derived
    # `answer` must never be promoted to a legacy answer on the next read.
    page = tmp_path / 'page.html'
    write_item(page, transcript)
    rows = [{'key': 'p/a', 'quotes': [{'qid': 'q1', 'quote_start': 7, 'quote_end': 12}]}]
    prior = {'qid': 'q1', 'answer': 'other', 'review_method': 'range_review_v2', 'legacy_answer': None}
    out = spans.reconcile_quote_reviews({'attribution': {'q1': prior}, 'spans': {'p/a': value}}, page, rows)
    assert out['attribution']['q1']['legacy_answer'] is None
    assert out['attribution']['q1']['answer'] == 'subject'
