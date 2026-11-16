"""Synthetic model-visible inputs only; private examples stay outside Git."""
CASES = [
    {"id": "known_name", "text": "Um, my name is Jan.",
     "speakers": [{"id": "participant", "description": "Jane Roe, interview participant"}]},
    {"id": "correct_name", "text": "Um, my name is Jan.",
     "speakers": [{"id": "participant", "description": "Jan, interview participant"}]},
    {"id": "ambiguous_name", "text": "Um, my name is Jan.",
     "speakers": [{"id": "participant", "description": "Interview participant; name is not known"}]},
]
