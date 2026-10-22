"""Caption correction persistence, stable anchors and conflict handling.

Ported from verbatim-index `scripts/test_annotations_corrections.py`. The HTTP
round trip will live in `test_serve.py` with the server port.
"""
from __future__ import annotations

import copy
import json

import pytest

from transcript_labeling.corrections import (corrected_text, export_corrections, update_corrections,
                                             validate_corrections)
from transcript_labeling.spans import build_sidecar, sidecar_path

RAW = '[00:00:01] How does this apply to the the brush argument again? >> Reply here'
EDIT = {'start': 6, 'end': 9, 'original': 'the the brush', 'replacement': 'the budget'}


@pytest.fixture
def transcript():
    return build_sidecar('p/a', RAW, [])


@pytest.fixture
def value(transcript):
    v = {k: transcript[k] for k in ('key', 'text_sha256', 'token_count')}
    v.update(edits=[dict(EDIT)], checked_by='operator', revision=0)
    return v


def test_replacement_changes_word_count_without_mutating_source(transcript, value):
    before = copy.deepcopy(transcript)
    saved = update_corrections(value, None, transcript)
    assert 'apply to the budget argument' in corrected_text(transcript, saved)
    assert transcript == before
    assert (saved['edits'][0]['start'], saved['edits'][0]['end']) == (6, 9)


@pytest.mark.parametrize("kind", ['stale', 'overlap', 'original', 'empty', 'bounds', 'boolean', 'timestamp', 'gap',
                                  'unchanged', 'too_long'])
def test_validation_rejects_wrong_source_overlap_and_metadata(transcript, value, kind):
    if kind == 'stale': value['text_sha256'] = 'old'
    elif kind == 'overlap': value['edits'].append(copy.deepcopy(EDIT))
    elif kind == 'original': value['edits'][0]['original'] = 'wrong'
    elif kind == 'empty': value['edits'][0]['replacement'] = ' '
    elif kind == 'bounds': value['edits'][0]['end'] = 999
    elif kind == 'boolean': value['edits'][0]['start'] = True
    elif kind == 'unchanged': value['edits'][0]['replacement'] = 'the the brush'
    elif kind == 'too_long': value['edits'][0]['replacement'] = 'x' * 4001
    else: value['edits'][0].update(start=0 if kind == 'timestamp' else 11, end=12)
    with pytest.raises(ValueError):
        validate_corrections(value, transcript)


def test_revisions_and_restore_history(transcript, value):
    first = update_corrections(value, None, transcript)
    with pytest.raises(ValueError, match='another window'):
        update_corrections(value, first, transcript)
    restored = update_corrections({**value, 'revision': 1, 'edits': [], 'history': ['forged']}, first, transcript)
    assert restored['history'][1]['before'] == [EDIT]
    assert restored['edits'] == []
    assert len(restored['history']) == 2
    assert corrected_text(transcript, restored) == ' '.join(transcript['tokens'])


def test_export_writes_corrected_text_and_leaves_the_sidecar(tmp_path, transcript, value):
    page = tmp_path / 'page.html'
    side = sidecar_path(page, 'p/a')
    side.parent.mkdir(parents=True)
    side.write_text(json.dumps(transcript))
    source = side.read_bytes()
    answers = tmp_path / 'human_answers.json'
    answers.write_text(json.dumps({'labels': {}, 'attribution': {},
                                   'corrections': {'p/a': update_corrections(value, None, transcript)}}))
    result = export_corrections(answers, page, tmp_path / 'export.json')
    assert 'the budget argument' in result['recordings']['p/a']['corrected_text']
    assert json.loads((tmp_path / 'export.json').read_text()) == result
    assert side.read_bytes() == source
