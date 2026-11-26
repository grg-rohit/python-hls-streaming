import copy
import os
import unittest
import sys
from types import SimpleNamespace
from unittest.mock import patch
from app import repository as repo

class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.catalog = {'version': 2, 'videos': {}, 'jobs': {}}
        self.env = patch.dict(os.environ, {'HYBRID_DEPLOYMENT': 'true'})
        self.env.start(); self.addCleanup(self.env.stop)
        def save(c): self.catalog = copy.deepcopy(c)
        for name, fn in [('_read_catalog', lambda: copy.deepcopy(self.catalog)), ('_write_catalog', save)]:
            p = patch.object(repo, name, side_effect=fn); p.start(); self.addCleanup(p.stop)
        for slug in ('normal','ai','legacy'):
            repo.upsert_video(slug, slug+'.mp4', slug+'.mp4', 10)

    def test_split_and_offline_gpu(self):
        normal = repo.create_job('normal', mode='gpu')
        ai = repo.create_job('ai', job_type='ai_upscale', options={'device':'cpu','scale':2})
        self.assertEqual((normal['mode'], normal['required_capability']), ('cpu','cpu'))
        self.assertEqual((ai['options']['device'], ai['required_capability']), ('cuda','gpu'))
        self.assertEqual(repo.claim_next_job(capabilities=['cpu'], claim_id='c1')['id'], normal['id'])
        self.assertIsNone(repo.claim_next_job(capabilities=['cpu'], claim_id='c2'))
        self.assertEqual(repo.get_job(ai['id'])['status'], 'queued')
        self.assertEqual(repo.claim_next_job(capabilities=['gpu'], claim_id='g1')['id'], ai['id'])
        self.assertIsNone(repo.claim_next_job(capabilities=['gpu'], claim_id='g2'))
        self.assertEqual(repo.claim_next_job(capabilities=['gpu'], claim_id='g1')['id'], ai['id'])

    def test_legacy_pending_ai(self):
        with patch.dict(os.environ, {'HYBRID_DEPLOYMENT':'false'}):
            old = repo.create_job('legacy', job_type='ai_upscale', options={'device':'auto'})
        self.assertIsNone(repo.claim_next_job(capabilities=['cpu'], claim_id='c1'))
        claimed = repo.claim_next_job(capabilities=['gpu'], claim_id='g1')
        self.assertEqual(claimed['id'], old['id'])
        self.assertEqual(claimed['options']['device'], 'cuda')

    def test_standalone_unchanged(self):
        with patch.dict(os.environ, {'HYBRID_DEPLOYMENT':'false'}):
            self.assertEqual(repo.job_routing('gpu','hls')[2], 'gpu')
            self.assertEqual(repo.job_routing('auto','ai_upscale', {'device':'auto'})[2], 'cpu')

class WorkerCapabilityTests(unittest.TestCase):
    def check(self, device, cuda, configured):
        import worker
        fake_torch = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: cuda, get_device_name=lambda _: 'Mock GPU'))
        with patch.dict(sys.modules, {'torch': fake_torch}), patch.object(worker, 'WORKER_DEVICE', device), patch.dict(os.environ, {'WORKER_CAPABILITIES': configured}):
            return worker.resolve_worker_device()

    def test_gpu_only(self):
        self.assertEqual(self.check('cuda', True, 'gpu'), ('cuda', ['gpu'], 'Mock GPU'))

    def test_cpu_only(self):
        self.assertEqual(self.check('cpu', True, 'cpu'), ('cpu', ['cpu'], None))

    def test_cuda_missing(self):
        with self.assertRaises(RuntimeError): self.check('cuda', False, 'gpu')

    def test_invalid_capability(self):
        with self.assertRaises(RuntimeError): self.check('cpu', False, 'gpu')

    def test_default_gpu_unchanged(self):
        self.assertEqual(self.check('cuda', True, '')[1], ['cpu', 'gpu'])

if __name__ == '__main__': unittest.main()
