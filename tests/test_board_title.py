"""Editable job titles and safe refresh previews."""
import io
import json
from unittest import mock
from tests.test_board_posting_url import PostingUrlTest, FREEHIRE, entry
from tools.board import server, add_job, state


class JobTitleTest(PostingUrlTest):
    def setUp(self):
        super().setUp()
        self.write(state.load())
        self.before = state.load()[FREEHIRE]

    def test_manual_title_preserves_other_fields(self):
        before = self.before
        code, result = self.post({'url': FREEHIRE, 'title': '  Graduate AI Engineer  '})
        self.assertEqual(code, 200)
        self.assertEqual(result['title'], 'Graduate AI Engineer')
        saved = state.load()[FREEHIRE]
        self.assertEqual(saved['title'], 'Graduate AI Engineer')
        self.assertEqual({k: v for k, v in saved.items() if k != 'title'},
                         {k: v for k, v in before.items() if k != 'title'})

    def test_invalid_title_does_not_change_status(self):
        for title in ('', ' '*4, 'a'*201, None, ['Engineer'],
                      'Sorry, Internet Explorer 11 is no longer supported by SmartRecruiters'):
            code, _ = self.post({'url': FREEHIRE, 'title': title, 'status': 'no'})
            self.assertEqual(code, 400)
            self.assertEqual(state.load()[FREEHIRE], self.before)

    def refresh(self):
        handler = object.__new__(server.Handler)
        handler.path = '/api/jobs/title?t=' + server.TOKEN
        body = json.dumps({'url': FREEHIRE}).encode()
        handler.headers = {'Content-Length': str(len(body))}
        handler.rfile = io.BytesIO(body)
        replies = []
        handler._send = lambda code, data: replies.append((code, json.loads(data)))
        handler.do_POST()
        return replies[0]

    def test_refresh_is_preview_only(self):
        with mock.patch.object(add_job, 'resolve', return_value={'title': 'AI Engineer II'}):
            code, result = self.refresh()
        self.assertEqual((code, result), (200, {'title': 'AI Engineer II'}))
        self.assertEqual(state.load()[FREEHIRE], self.before)

    def test_refresh_rejects_banner_and_handles_fetch_failure(self):
        with mock.patch.object(add_job, 'resolve', return_value={'title': 'Access denied'}):
            self.assertEqual(self.refresh()[0], 422)
        with mock.patch.object(add_job, 'resolve', side_effect=RuntimeError('failure')):
            self.assertEqual(self.refresh()[0], 502)
        self.assertEqual(state.load()[FREEHIRE], self.before)

    def test_old_bookmarklet_cannot_import_error_title(self):
        with mock.patch.object(add_job, 'resolve', return_value={}), mock.patch.object(add_job, '_write') as write:
            code, result = add_job.add({'job_url': FREEHIRE, 'company': 'Example',
                                      'title': 'Sorry, Internet Explorer 11 is no longer supported',
                                      'allow_no_description': True})
        self.assertEqual(code, 422)
        self.assertIn('title', result['need'])
        write.assert_not_called()
