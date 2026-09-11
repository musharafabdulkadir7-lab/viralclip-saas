import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
os.environ.setdefault('WORKER_SECRET', 'test-secret')

import pytest
from app.services import job_queue


@pytest.mark.asyncio
async def test_enqueue_claim_ack_roundtrip(fake_redis, monkeypatch):
    monkeypatch.setattr(job_queue, 'get_redis', lambda: fake_redis)
    job_id = await job_queue.enqueue({'user_id': 'u1', 'niche': 'finance'})
    job = await job_queue.claim_next('consumer-1')
    assert job is not None and job.job_id == job_id
    await job_queue.set_status(job.job_id, 'processing', 5, 'Processing...')
    await job_queue.ack(job)
    status = await job_queue.get_status(job_id)
    assert status['status'] == 'processing'


@pytest.mark.asyncio
async def test_reclaim_after_max_attempts_dead_letters(fake_redis, monkeypatch):
    monkeypatch.setattr(job_queue, 'get_redis', lambda: fake_redis)
    monkeypatch.setattr(job_queue, 'CLAIM_IDLE_MS', 0)  # force-eligible immediately
    job_id = await job_queue.enqueue({'user_id': 'u1', 'niche': 'gaming'})
    for i in range(job_queue.MAX_ATTEMPTS + 1):
        await job_queue.claim_next(f'consumer-{i}')  # never acked, stays pending
    dead_len = await fake_redis.xlen(job_queue.DEAD_LETTER)
    assert dead_len == 1


@pytest.mark.asyncio
async def test_job_owner_roundtrip(fake_redis, monkeypatch):
    monkeypatch.setattr(job_queue, 'get_redis', lambda: fake_redis)
    job_id = await job_queue.enqueue({'user_id': 'u42', 'niche': 'cooking'})
    assert await job_queue.get_owner(job_id) == 'u42'
