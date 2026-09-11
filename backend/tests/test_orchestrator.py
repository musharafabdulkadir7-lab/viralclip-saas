import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
os.environ.setdefault('WORKER_SECRET', 'test-secret')

from pipeline.orchestrator import ClipJob


def test_split_screen_requires_broll():
    job = ClipJob(mode='public_domain', user_id='u1', job_id='j1', niche='x', layout='split_screen')
    assert any('broll_path' in p for p in job.validate())


def test_num_clips_clamped_to_max(monkeypatch):
    monkeypatch.setattr('pipeline.config.settings.max_clips_per_job', 5)
    job = ClipJob.from_queue_payload({'user_id': 'u1', 'job_id': 'j1', 'num_clips': 99})
    assert job.num_clips == 5


def test_unknown_mode_rejected():
    job = ClipJob(mode='not_a_real_mode', user_id='u1', job_id='j1')
    assert any('Unknown mode' in p for p in job.validate())
