"""Regression coverage for template history, drafts, references and local routes."""
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

import app


class WorkbenchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        app.DATA_DIR = app.Path(cls.tmp.name)
        app.DB_PATH = app.DATA_DIR / "test.sqlite3"
        cls.server = app.make_server(0)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)
        cls.tmp.cleanup()

    def req(self, path, payload=None, origin=None):
        headers = {}
        if payload is not None:
            headers["Content-Type"] = "application/json"
            payload = json.dumps(payload, ensure_ascii=False).encode()
        if origin: headers["Origin"] = origin
        req = urllib.request.Request(self.base + path, data=payload, headers=headers,
                                     method="POST" if payload is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=8) as res:
                content = res.read()
                return res.status, json.loads(content) if res.headers['Content-Type'].startswith('application/json') else content
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())

    def test_01_initial_templates(self):
        status, data = self.req('/api/templates')
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(data), 9)
        self.assertEqual({t['platform'] for t in data}, set(app.PLATFORMS))

    def test_02_template_version_is_immutable(self):
        _, items = self.req('/api/templates')
        template = items[0]
        status, newer = self.req(f"/api/templates/{template['id']}/versions", {'prompt': '新增写作规则'})
        self.assertEqual(status, 201)
        self.assertEqual(newer['version'], 2)
        _, history = self.req(f"/api/templates/{template['id']}/versions")
        self.assertEqual(history[0]['prompt'], '新增写作规则')
        self.assertEqual(history[1]['prompt'], template['prompt'])

    def test_03_draft_save_and_update(self):
        _, created = self.req('/api/drafts', {'title':'测试文章','platform':'知乎','content':'草稿第一版'})
        did = created['id']
        self.req('/api/drafts', {'id':did,'title':'更新标题','platform':'知乎','content':'修改后内容'})
        _, drafts = self.req('/api/drafts')
        self.assertEqual(next(d for d in drafts if d['id']==did)['content'], '修改后内容')

    def test_04_import_reference(self):
        status, data = self.req('/api/references', {'title':'参考文章','platform':'知乎','content':'第一段\n第二段'})
        self.assertEqual(status, 201)
        self.assertEqual(data['analysis']['段落数'], 2)

    def test_05_missing_content_rejected(self):
        status, data = self.req('/api/drafts', {'title':'不完整','platform':'知乎'})
        self.assertEqual(status, 400)
        self.assertIn('content', data['error'])

    def test_06_manual_schedule_platform_match(self):
        _, draft = self.req('/api/drafts', {'title':'小红书稿件','platform':'小红书','content':'内容'})
        _, account = self.req('/api/accounts', {'label':'测试账号','platform':'小红书'})
        status, task = self.req('/api/tasks', {'draft_id':draft['id'],'account_id':account['id'],'scheduled_at':'2026-11-01T10:30'})
        self.assertEqual(status, 201)
        self.assertEqual(task['mode'], 'manual')

    def test_07_schedule_rejects_cross_platform(self):
        _, draft = self.req('/api/drafts', {'title':'公众号稿件','platform':'微信公众号','content':'内容'})
        _, account = self.req('/api/accounts', {'label':'小红书','platform':'小红书'})
        status, data = self.req('/api/tasks', {'draft_id':draft['id'],'account_id':account['id'],'scheduled_at':'2026-11-01T10:30'})
        self.assertEqual(status, 400)

    def test_08_cloud_default_off(self):
        original = os.environ.pop('GEMINI_ALLOW_REQUESTS', None)
        try:
            with self.assertRaisesRegex(ValueError, '默认关闭'):
                app.call_model('测试','gemini','')
        finally:
            if original is not None: os.environ['GEMINI_ALLOW_REQUESTS'] = original

    def test_09_host_guard(self):
        req = urllib.request.Request(self.base + '/api/health', headers={'Host':'bad.example'})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(cm.exception.code,403)

    def test_10_origin_guard(self):
        status, _ = self.req('/api/drafts', {'title':'x'}, origin='https://evil.example')
        self.assertEqual(status,403)

    def test_11_http_page(self):
        code, raw = self.req('/')
        self.assertEqual(code,200)
        self.assertIn('Creator Workbench'.encode(),raw)

    def test_12_experiment_export_missing(self):
        code, _ = self.req('/api/experiments/999999/export')
        self.assertEqual(code,404)


if __name__ == '__main__':
    unittest.main()
