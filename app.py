"""Creator Workbench — dependency-free, localhost-only prototype."""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("CREATOR_WORKBENCH_DATA", ROOT / "data"))
DB_PATH = DATA_DIR / "workbench.sqlite3"
WEB = ROOT / "public"
PLATFORMS = ("小红书", "微信公众号", "知乎")
STYLES = (
    ("实用干货", "以用户的真实问题开头，给出可执行步骤，提供避坑提醒，最后给出一条行动建议。"),
    ("故事叙事", "先用一个有张力的真实情境引入，再解释变化与收获，最后邀请读者分享经验。不得编造事实。"),
    ("测评分析", "先说明评估标准，再按优缺点进行比较，给出适用人群和结论；未知数据明确标注。"),
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    try:
        with c:
            yield c
    finally:
        c.close()


def init_db() -> None:
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS templates (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, platform TEXT NOT NULL,
            category TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS template_versions (
            id INTEGER PRIMARY KEY, template_id INTEGER NOT NULL REFERENCES templates(id),
            version INTEGER NOT NULL, prompt TEXT NOT NULL, created_at TEXT NOT NULL,
            UNIQUE(template_id,version));
        CREATE TABLE IF NOT EXISTS drafts (
            id INTEGER PRIMARY KEY, title TEXT NOT NULL, platform TEXT NOT NULL,
            content TEXT NOT NULL, template_version_id INTEGER REFERENCES template_versions(id),
            updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS reference_articles (
            id INTEGER PRIMARY KEY, title TEXT NOT NULL, platform TEXT NOT NULL,
            url TEXT NOT NULL DEFAULT '', content TEXT NOT NULL,
            analysis TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS experiments (
            id INTEGER PRIMARY KEY, topic TEXT NOT NULL, template_a INTEGER NOT NULL,
            template_b INTEGER NOT NULL, output_a TEXT NOT NULL, output_b TEXT NOT NULL,
            notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS platform_accounts (
            id INTEGER PRIMARY KEY, platform TEXT NOT NULL, label TEXT NOT NULL,
            mode TEXT NOT NULL DEFAULT 'manual', created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS publish_tasks (
            id INTEGER PRIMARY KEY, draft_id INTEGER NOT NULL REFERENCES drafts(id),
            account_id INTEGER NOT NULL REFERENCES platform_accounts(id),
            scheduled_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'scheduled',
            created_at TEXT NOT NULL);
        """)
        if c.execute("SELECT COUNT(*) FROM templates").fetchone()[0] == 0:
            for platform in PLATFORMS:
                for category, instructions in STYLES:
                    suffix = {"小红书":"适当分段、少量表情符号和话题标签；避免夸大效果。",
                              "微信公众号":"采用完整段落、小标题和摘要，适合手机阅读。",
                              "知乎":"先回答核心问题，再提供论据和边界条件。"}[platform]
                    prompt = f"你是严谨的{platform}作者。{instructions}{suffix} 要求原创、真实，不得编造数据或经历。"
                    cur = c.execute("INSERT INTO templates(name,platform,category,created_at) VALUES(?,?,?,?)",
                                    (f"{platform} · {category}", platform, category, now()))
                    c.execute("INSERT INTO template_versions(template_id,version,prompt,created_at) VALUES(?,?,?,?)",
                              (cur.lastrowid, 1, prompt, now()))


def rows(query: str, args: tuple = ()) -> list[dict]:
    with db() as c:
        return [dict(r) for r in c.execute(query, args)]


def one(query: str, args: tuple = ()) -> dict | None:
    result = rows(query, args)
    return result[0] if result else None


def require_text(obj: dict, field: str, limit: int = 10000) -> str:
    value = obj.get(field)
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{field} 必须是非空文本，最长 {limit} 字符")
    return value.strip()


def require_id(obj: dict, field: str) -> int:
    value = obj.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} 必须是正整数")
    return value


def template_versions(template_id: int) -> list[dict]:
    return rows("SELECT * FROM template_versions WHERE template_id=? ORDER BY version DESC", (template_id,))


def latest_version(template_id: int) -> dict:
    version = one("SELECT * FROM template_versions WHERE template_id=? ORDER BY version DESC LIMIT 1", (template_id,))
    if not version:
        raise ValueError("模板不存在")
    return version


def compose(topic: str, version: dict) -> str:
    template = one("SELECT * FROM templates WHERE id=?", (version["template_id"],))
    return (f"你必须遵循以下创作规则：\n{version['prompt']}\n\n"
            f"目标平台：{template['platform']}\n选题：{topic}\n"
            "请输出一个吸引人的标题和完整原创正文，区分已知事实和待核实信息。")


def call_model(prompt: str, provider: str, model: str) -> str:
    if provider == "ollama":
        base = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
        if base != "http://127.0.0.1:11434" and not base.startswith("http://localhost:"):
            raise ValueError("仅允许连接本机 Ollama 服务")
        endpoint = f"{base}/api/generate"
        payload = {"model": model or "qwen2.5:7b", "prompt": prompt, "stream": False}
        headers = {"Content-Type":"application/json"}
    elif provider == "gemini":
        if os.environ.get("GEMINI_ALLOW_REQUESTS") != "1":
            raise ValueError("Gemini 请求默认关闭。确认免费层和数据政策后设置 GEMINI_ALLOW_REQUESTS=1")
        key = os.environ.get("GEMINI_API_KEY", "")
        if not key:
            raise ValueError("未设置 GEMINI_API_KEY")
        selected = model or "gemini-2.5-flash"
        if not re.fullmatch(r"[a-zA-Z0-9_.-]+", selected):
            raise ValueError("模型名称无效")
        endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{selected}:generateContent"
        payload = {"contents":[{"parts":[{"text":prompt}]}]}
        headers = {"Content-Type":"application/json", "x-goog-api-key":key}
    else:
        raise ValueError("暂支持 ollama 或 gemini")
    req = urllib.request.Request(endpoint, data=json.dumps(payload).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            result = json.load(resp)
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ValueError(f"模型调用失败：{exc.reason if hasattr(exc, 'reason') else exc}") from exc
    if provider == "ollama":
        result_text = result.get("response", "")
    else:
        result_text = "\n".join(part.get("text", "") for candidate in result.get("candidates", [])
                                 for part in candidate.get("content", {}).get("parts", []))
    if not result_text.strip():
        raise ValueError("模型未返回可用文本")
    return result_text


def extract_structure(content: str) -> dict:
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    return {"字数":len(content), "段落数":len(lines),
            "开头示例":lines[0][:90] if lines else "", "结尾示例":lines[-1][:90] if lines else "",
            "分析提示":"可学习结构与表达手法；请勿照搬原文或将热度归因当成因果结论。"}


class Handler(BaseHTTPRequestHandler):
    server_version = "CreatorWorkbench/0.1"

    def log_message(self, fmt, *args):
        pass

    def send_data(self, payload, status=200, content_type="application/json", filename=None):
        if isinstance(payload, (dict,list)):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        elif isinstance(payload, str):
            body = payload.encode("utf-8")
        else:
            body = payload
        self.send_response(status)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        if filename:
            self.send_header("Content-Disposition",f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.headers.get("Host", "").split(":")[0] not in ("127.0.0.1", "localhost"):
            return self.send_data({"error":"Host 无效"},403)
        path = urlparse(self.path).path
        try:
            if path == "/api/health": return self.send_data({"ok":True,"version":"0.1"})
            if path == "/api/overview":
                return self.send_data({table:one(f"SELECT COUNT(*) AS count FROM {table}")["count"] for table in
                    ("templates","drafts","reference_articles","experiments","platform_accounts","publish_tasks")})
            if path == "/api/templates":
                return self.send_data(rows("SELECT t.*,v.version,v.id AS version_id,v.prompt FROM templates t JOIN template_versions v ON v.id=(SELECT id FROM template_versions WHERE template_id=t.id ORDER BY version DESC LIMIT 1) ORDER BY t.id"))
            m = re.fullmatch(r"/api/templates/(\d+)/versions",path)
            if m: return self.send_data(template_versions(int(m.group(1))))
            if path == "/api/drafts": return self.send_data(rows("SELECT * FROM drafts ORDER BY updated_at DESC"))
            if path == "/api/references": return self.send_data(rows("SELECT * FROM reference_articles ORDER BY id DESC"))
            if path == "/api/experiments": return self.send_data(rows("SELECT * FROM experiments ORDER BY id DESC"))
            m = re.fullmatch(r"/api/experiments/(\d+)/export",path)
            if m:
                record = one("SELECT * FROM experiments WHERE id=?",(int(m.group(1)),))
                if not record: return self.send_data({"error":"实验不存在"},404)
                return self.send_data(record,filename=f"experiment-{record['id']}.json")
            if path == "/api/accounts": return self.send_data(rows("SELECT * FROM platform_accounts ORDER BY id DESC"))
            if path == "/api/tasks":
                return self.send_data(rows("SELECT p.*,d.title,a.platform,a.label FROM publish_tasks p JOIN drafts d ON d.id=p.draft_id JOIN platform_accounts a ON a.id=p.account_id ORDER BY p.scheduled_at"))
            if path == "/api/config":
                return self.send_data({"ollama":"本机模型 (Ollama)","gemini_enabled":os.environ.get("GEMINI_ALLOW_REQUESTS")=="1",
                                       "gemini_key_configured":bool(os.environ.get("GEMINI_API_KEY")),"cloud_budget":"默认关闭，需要明确启用"})
            if path == "/": path="/index.html"
            if path in ("/index.html","/style.css","/app.js"):
                f = WEB / path.lstrip("/")
                typ = {".html":"text/html",".css":"text/css",".js":"application/javascript"}[f.suffix]
                return self.send_data(f.read_bytes(),content_type=typ)
            return self.send_data({"error":"路由不存在"},404)
        except (ValueError, sqlite3.Error) as exc:
            return self.send_data({"error":str(exc)},400)

    def do_POST(self):
        if self.headers.get("Host", "").split(":")[0] not in ("127.0.0.1", "localhost"):
            return self.send_data({"error":"Host 无效"},403)
        origin = self.headers.get("Origin")
        if origin and origin not in (f"http://127.0.0.1:{self.server.server_port}",f"http://localhost:{self.server.server_port}"):
            return self.send_data({"error":"跨站请求被拒绝"},403)
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self.send_data({"error":"需要 application/json"},415)
        try:
            size = int(self.headers.get("Content-Length","0"))
            if size < 1 or size > 1024*1024: raise ValueError("请求体大小无效")
            body = json.loads(self.rfile.read(size))
            if not isinstance(body,dict): raise ValueError("请求格式无效")
            path = urlparse(self.path).path
            with db() as c:
                if path == "/api/templates":
                    name = require_text(body,"name",120); platform = require_text(body,"platform",50)
                    category = require_text(body,"category",60); prompt = require_text(body,"prompt")
                    t = c.execute("INSERT INTO templates(name,platform,category,created_at) VALUES(?,?,?,?)",(name,platform,category,now())).lastrowid
                    v = c.execute("INSERT INTO template_versions(template_id,version,prompt,created_at) VALUES(?,?,?,?)",(t,1,prompt,now())).lastrowid
                    return self.send_data({"id":t,"version_id":v},201)
                m = re.fullmatch(r"/api/templates/(\d+)/versions",path)
                if m:
                    tid=int(m.group(1)); prompt=require_text(body,"prompt"); v=latest_version(tid)
                    version=v["version"]+1
                    vid=c.execute("INSERT INTO template_versions(template_id,version,prompt,created_at) VALUES(?,?,?,?)",(tid,version,prompt,now())).lastrowid
                    return self.send_data({"id":vid,"version":version},201)
                if path == "/api/generate":
                    topic=require_text(body,"topic",500)
                    vid=require_id(body,"version_id")
                    v=one("SELECT * FROM template_versions WHERE id=?",(vid,))
                    if not v: raise ValueError("模板版本不存在")
                    result=call_model(compose(topic,v),body.get("provider","ollama"),str(body.get("model", "")))
                    return self.send_data({"text":result,"version_id":vid})
                if path == "/api/rewrite":
                    content=require_text(body,"content",60000); instruction=require_text(body,"instruction",500)
                    prompt=f"请按要求修改以下原创稿件，保留事实，不增加无法核实的断言。要求：{instruction}\n\n{content}"
                    result=call_model(prompt,body.get("provider","ollama"),str(body.get("model", "")))
                    return self.send_data({"text":result})
                if path == "/api/drafts":
                    title=require_text(body,"title",160); platform=require_text(body,"platform",50)
                    content=require_text(body,"content",60000)
                    vid=body.get("version_id"); vid=vid if isinstance(vid,int) and vid>0 else None
                    if vid and not one("SELECT id FROM template_versions WHERE id=?",(vid,)):
                        raise ValueError("模板版本不存在")
                    did=body.get("id")
                    if did:
                        did=require_id(body,"id")
                        if not c.execute("UPDATE drafts SET title=?,platform=?,content=?,template_version_id=?,updated_at=? WHERE id=?",(title,platform,content,vid,now(),did)).rowcount:
                            raise ValueError("草稿不存在")
                    else:
                        did=c.execute("INSERT INTO drafts(title,platform,content,template_version_id,updated_at) VALUES(?,?,?,?,?)",(title,platform,content,vid,now())).lastrowid
                    return self.send_data({"id":did})
                if path == "/api/references":
                    title=require_text(body,"title",160); platform=require_text(body,"platform",50)
                    content=require_text(body,"content",60000); url=str(body.get("url", ""))[:2000]
                    analysis=extract_structure(content)
                    rid=c.execute("INSERT INTO reference_articles(title,platform,url,content,analysis,created_at) VALUES(?,?,?,?,?,?)",
                                  (title,platform,url,content,json.dumps(analysis,ensure_ascii=False),now())).lastrowid
                    return self.send_data({"id":rid,"analysis":analysis},201)
                if path == "/api/experiments":
                    topic=require_text(body,"topic",500)
                    aid=require_id(body,"template_a");bid=require_id(body,"template_b")
                    a=one("SELECT * FROM template_versions WHERE id=?",(aid,));b=one("SELECT * FROM template_versions WHERE id=?",(bid,))
                    if not a or not b: raise ValueError("模板版本不存在")
                    provider=body.get("provider","ollama");model=str(body.get("model", ""))
                    out_a=call_model(compose(topic,a),provider,model)
                    out_b=call_model(compose(topic,b),provider,model)
                    eid=c.execute("INSERT INTO experiments(topic,template_a,template_b,output_a,output_b,created_at) VALUES(?,?,?,?,?,?)",(topic,aid,bid,out_a,out_b,now())).lastrowid
                    return self.send_data({"id":eid,"output_a":out_a,"output_b":out_b},201)
                if path == "/api/accounts":
                    platform=require_text(body,"platform",50);label=require_text(body,"label",100)
                    aid=c.execute("INSERT INTO platform_accounts(platform,label,created_at) VALUES(?,?,?)",(platform,label,now())).lastrowid
                    return self.send_data({"id":aid,"mode":"manual"},201)
                if path == "/api/tasks":
                    did=require_id(body,"draft_id"); aid=require_id(body,"account_id")
                    scheduled=require_text(body,"scheduled_at",40)
                    try: datetime.fromisoformat(scheduled.replace("Z","+00:00"))
                    except ValueError: raise ValueError("日期格式无效")
                    account=one("SELECT * FROM platform_accounts WHERE id=?",(aid,))
                    draft=one("SELECT * FROM drafts WHERE id=?",(did,))
                    if not account or not draft: raise ValueError("账号或草稿不存在")
                    if account["platform"] != draft["platform"]: raise ValueError("账号平台与草稿不一致")
                    pid=c.execute("INSERT INTO publish_tasks(draft_id,account_id,scheduled_at,created_at) VALUES(?,?,?,?)",(did,aid,scheduled,now())).lastrowid
                    return self.send_data({"id":pid,"status":"scheduled","mode":"manual"},201)
            return self.send_data({"error":"路由不存在"},404)
        except (ValueError,sqlite3.Error,TypeError,json.JSONDecodeError) as exc:
            return self.send_data({"error":str(exc)},400)


def make_server(port: int = 8754) -> ThreadingHTTPServer:
    init_db()
    return ThreadingHTTPServer(("127.0.0.1",port),Handler)


if __name__ == "__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--port",type=int,default=8754)
    args=parser.parse_args()
    server=make_server(args.port)
    print(f"Creator Workbench: http://127.0.0.1:{server.server_port}/",flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()
