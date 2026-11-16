"""Held-out expectations. Never import this in request preparation or an adapter."""
EXPECTED = {"known_name": [(4, 5, "Jan.", "Jane.")], "correct_name": [], "ambiguous_name": []}


def score(case_id, bundle):
    actual = [(p["start"], p["end"], p["original"], p["replacement"]) for p in bundle["text_suggestions"]]
    errors = []
    if actual != EXPECTED[case_id]:
        errors.append("wrong_text_corrections")
    if bundle["ranges"] != [{"start": 0, "end": 5, "speaker": "participant", "source": "contextual_model"}]:
        errors.append("wrong_speaker_ranges")
    return errors
