from worker import ClipJob


def test_own_content_requires_source_kind():
    job = ClipJob(mode="own_content", user_id="u1", job_id="j1", source_kind=None)
    problems = job.validate()
    assert any("source_kind" in p for p in problems)


def test_split_screen_requires_broll_path():
    job = ClipJob(mode="own_content", user_id="u1", job_id="j1", source_kind="file",
                  source="/tmp/x.mp4", layout="split_screen", broll_path=None)
    problems = job.validate()
    assert any("broll_path" in p for p in problems)


def test_split_screen_with_broll_path_ok_besides_other_checks():
    job = ClipJob(mode="own_content", user_id="u1", job_id="j1", source_kind="file",
                  source="/tmp/x.mp4", layout="split_screen", broll_path="/tmp/broll.mp4")
    problems = job.validate()
    assert not any("broll_path" in p for p in problems)


def test_unknown_mode_flagged():
    job = ClipJob(mode="not_a_real_mode", user_id="u1", job_id="j1")
    problems = job.validate()
    assert any("Unknown mode" in p for p in problems)


def test_valid_own_content_file_job_has_no_source_kind_problem():
    job = ClipJob(mode="own_content", user_id="u1", job_id="j1", source_kind="channel", source="abc123")
    problems = job.validate()
    assert not any("source_kind" in p for p in problems)
