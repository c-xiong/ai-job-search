"""Full job edits persist bodies and preserve row identity."""
import io
import json
from unittest import mock
from tests.test_board_title import JobTitleTest
from tests.test_board_posting_url import FREEHIRE
from tools.board import add_job, server, state


class JobEditTest(JobTitleTest):
    def test_full_job_and_body(self):
        body = 'Build reliable software.\n\nRequirements: Python and testing.'
        with mock.patch.object(server.fit_score, 'apply_to'):
            code, _ = self.post({'url': FREEHIRE, 'job': {
                'title': 'Graduate Software Engineer', 'company': 'Example Labs',
                'location': 'London', 'posted': '2026-10-01', 'deadline': '2026-11-01',
                'description': body}})
        self.assertEqual(code, 200)
        saved = state.load()[FREEHIRE]
        self.assertEqual(saved['job_id'], self.before['job_id'])
        self.assertEqual(saved['user_status'], self.before['user_status'])
        self.assertEqual(saved['user_note'], self.before['user_note'])
        self.assertEqual(saved['posting_source'], 'manual')
        self.assertEqual(state.job_payload(FREEHIRE)['description'], body)
        self.assertEqual(state.job_payload(FREEHIRE)['company'], 'Example Labs')

    def test_invalid_fields_do_not_write_anything(self):
        for fields in ({'title': 'New title', 'company': ''}, {'posted': '2026-02-31'},
                       {'description': 3}, {'description': 'x'*60001}, {'unknown': 'x'}):
            code, _ = self.post({'url': FREEHIRE, 'job': fields, 'status': 'no'})
            self.assertEqual(code, 400)
            self.assertEqual(state.load()[FREEHIRE], self.before)

    def test_explicit_clear_body(self):
        with mock.patch.object(server.fit_score, 'apply_to'):
            self.post({'url': FREEHIRE, 'job': {'description': 'Example body'}})
            code, _ = self.post({'url': FREEHIRE, 'job': {'description': ''}})
        self.assertEqual(code, 200)
        self.assertFalse(state.job_payload(FREEHIRE)['has_posting'])
        self.assertEqual(state.job_payload(FREEHIRE)['description'], '')

    def test_refresh_job_preview_does_not_write(self):
        handler = object.__new__(server.Handler)
        handler.path = '/api/jobs/refresh?t=' + server.TOKEN
        body = json.dumps({'url': FREEHIRE}).encode()
        handler.headers = {'Content-Length': str(len(body))}
        handler.rfile = io.BytesIO(body)
        replies = []
        handler._send = lambda code, data: replies.append((code, json.loads(data)))
        with mock.patch.object(add_job, 'resolve', return_value={'title': 'Engineer', 'description': 'New body', 'location': ''}):
            handler.do_POST()
        self.assertEqual(replies[0], (200, {'fields': {'title': 'Engineer', 'description': 'New body'}}))
        self.assertEqual(state.load()[FREEHIRE], self.before)

    def test_later_fetch_keeps_manual_body(self):
        with mock.patch.object(server.fit_score, 'apply_to'):
            self.post({'url': FREEHIRE, 'job': {'description': 'My corrected JD'}})
        saved = state.load()[FREEHIRE]
        pending = []
        with mock.patch.object(server.ats_fetch, '_rescore'):
            server.ats_fetch.enrich_existing(saved, {'url': FREEHIRE, 'title': saved['title'],
                'company': saved['company'], 'description': 'Fetched body ' * 300},
                FREEHIRE, '2026-10-21', 'freehire', pending, None)
        self.assertEqual(pending, [])
        self.assertEqual(state.postings.load(saved), 'My corrected JD')
