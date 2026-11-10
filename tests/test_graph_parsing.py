from blackbox.graph.parsing import parse_generation, parse_grade


def test_parse_grade_reads_plain_json():
    grade = parse_grade('{"sufficient": true, "reason": "the chunks answer it"}')
    assert grade.sufficient is True
    assert grade.reason == "the chunks answer it"


def test_parse_grade_reads_fenced_json():
    grade = parse_grade('```json\n{"sufficient": false, "reason": "weak"}\n```')
    assert grade.sufficient is False
    assert grade.reason == "weak"


def test_parse_grade_malformed_defaults_to_insufficient():
    grade = parse_grade("I think it is good enough")
    assert grade.sufficient is False
    assert "unparseable" in grade.reason.lower()


def test_parse_grade_missing_key_defaults_to_insufficient():
    grade = parse_grade('{"reason": "no verdict"}')
    assert grade.sufficient is False


def test_parse_generation_reads_structured_output():
    gen = parse_generation(
        '{"answer": "The pool was saturated [c1].", '
        '"claims": [{"text": "The pool was saturated", "sources": ["c1"]}]}'
    )
    assert gen.answer.startswith("The pool")
    assert gen.claims[0].sources == ["c1"]


def test_parse_generation_falls_back_to_sentences_and_citations():
    gen = parse_generation("The pool was saturated [c1]. Checkout slowed down [c1].")
    assert len(gen.claims) == 2
    assert gen.claims[0].sources == ["c1"]


def test_parse_generation_empty_text_is_empty():
    gen = parse_generation("")
    assert gen.answer == ""
    assert gen.claims == []
