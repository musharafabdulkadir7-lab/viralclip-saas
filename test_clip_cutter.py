from clip_cutter import parse_time, format_ass_time, _esc


def test_parse_time_hh_mm_ss():
    assert parse_time("00:01:30.500") == 90.5


def test_parse_time_mm_ss():
    assert parse_time("01:30.500") == 90.5


def test_format_ass_time_basic():
    assert format_ass_time(90.5) == "0:01:30.50"


def test_format_ass_time_negative_clamped_to_zero():
    assert format_ass_time(-5) == "0:00:00.00"


def test_esc_escapes_special_chars():
    assert _esc("it's: a, test") == "it\\'s\\: a\\, test"
