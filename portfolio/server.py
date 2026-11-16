"""HTTP 服务：REST API + 静态页面。仅依赖 Python 标准库。

运行：python3 server.py [port]   默认 8000
"""
from __future__ import annotations

import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import seed
import store

BASE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(BASE, "static")

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
}


class ApiError(Exception):
    def __init__(self, status, message, **extra):
        super().__init__(message)
        self.status = status
        self.extra = extra


def require(cond, status, message, **extra):
    if not cond:
        raise ApiError(status, message, **extra)


def handle_api(method, path, query, body):
    db = store.get_db()
    now = store.get_now(db)

    if method == "GET" and path == "/api/bootstrap":
        return {
            "now": now,
            "skills": seed.SKILLS,
            "scenarios": store.list_scenarios(db),
            "execution": store.list_execution(db),
        }

    m = re.fullmatch(r"/api/scenarios/(\d+)", path)
    if method == "GET" and m:
        return store.get_scenario(db, int(m.group(1)))

    if method == "POST" and path == "/api/scenarios":
        name = (body.get("name") or "新草案").strip()
        by = body.get("by") or "匿名"
        if body.get("from_id"):
            src = store.get_scenario(db, int(body["from_id"]))
            import copy
            assumptions = copy.deepcopy(src["assumptions"])
            parent = src["id"]
        else:
            assumptions = seed.base_assumptions()
            parent = None
        sid = store.create_scenario(db, name, assumptions, by, parent_id=parent)
        return store.get_scenario(db, sid)

    m = re.fullmatch(r"/api/scenarios/(\d+)", path)
    if method == "PUT" and m:
        sid = int(m.group(1))
        assumptions = body.get("assumptions")
        require(isinstance(assumptions, dict), 400, "缺少 assumptions")
        require(assumptions.get("people") and assumptions.get("projects"), 400, "假设必须包含 people 与 projects")
        try:
            return store.update_assumptions(db, sid, body.get("version"), assumptions, body.get("by") or "匿名")
        except store.VersionConflict as e:
            raise ApiError(409, str(e), current_version=e.current_version)
        except store.StateError as e:
            raise ApiError(400, str(e))

    m = re.fullmatch(r"/api/scenarios/(\d+)/compute", path)
    if method == "POST" and m:
        try:
            return store.recompute(db, int(m.group(1)))
        except store.StateError as e:
            raise ApiError(400, str(e))

    m = re.fullmatch(r"/api/scenarios/(\d+)/confirm", path)
    if method == "POST" and m:
        try:
            return store.confirm(db, int(m.group(1)), body.get("version"), body.get("by") or "匿名")
        except store.VersionConflict as e:
            raise ApiError(409, str(e), current_version=e.current_version)
        except store.StateError as e:
            raise ApiError(422, str(e))

    m = re.fullmatch(r"/api/scenarios/(\d+)/restore", path)
    if method == "POST" and m:
        sid = store.restore_as_draft(db, int(m.group(1)), body.get("by") or "匿名")
        return store.get_scenario(db, sid)

    if method == "GET" and path == "/api/compare":
        ids = [int(x) for x in (query.get("ids") or "").split(",") if x.strip()]
        out = []
        for sid in ids:
            row = store.get_scenario(db, sid)
            if not row["schedule"]:
                continue
            out.append({
                "id": row["id"],
                "name": row["name"],
                "status": row["status"],
                "version": row["version"],
                "strategy": row["assumptions"].get("strategy"),
                "portfolio": row["schedule"]["portfolio"],
                "projects": row["schedule"]["projects"],
                "people": row["schedule"]["people"],
                "horizon_end": row["schedule"]["horizon_end"],
            })
        return out

    if method == "GET" and path == "/api/execution":
        return {"now": now, "records": store.list_execution(db)}

    if method == "POST" and path == "/api/execution/start":
        # 把某方案假设中的一个阶段标记为"已开始"（从当前周 now 起）。
        # 之后任何方案若修改该阶段，确认时会被拒绝 —— 已开始的阶段不能被新方案悄悄移动。
        sid = int(body.get("scenario_id"))
        row = store.get_scenario(db, sid)
        pid, phid = body.get("project_id"), body.get("phase_id")
        uid = body.get("person_id")
        key = f"{pid}/{phid}"
        existing = {r["phase_key"] for r in store.list_execution(db)}
        require(key not in existing, 400, f"阶段 {key} 已有执行记录")
        proj = next((p for p in row["assumptions"]["projects"] if p["id"] == pid), None)
        require(proj, 400, f"方案 {sid} 中不存在项目 {pid}")
        phase = next((p for p in proj["phases"] if p["id"] == phid), None)
        require(phase, 400, f"项目 {pid} 中不存在阶段 {phid}")
        person = next((p for p in row["assumptions"]["people"] if p["id"] == uid), None)
        require(person, 400, f"方案 {sid} 中不存在人员 {uid}")
        lvl = person.get("skills", {}).get(phase["skill"], 0)
        require(lvl >= phase.get("min_level", 1), 400,
                f"{person['name']} 的 {phase['skill']} 等级 {lvl} 不满足要求 ≥{phase.get('min_level', 1)}")
        cap = float(person.get("capacity", 1.0) or 1.0)
        end = now + float(phase["effort_weeks"]) / cap
        store.add_execution(db, {
            "phase_key": key, "project_id": pid, "phase_id": phid, "person_id": uid,
            "start_week": now, "end_week": end, "status": "started",
            "note": body.get("note") or f"由 {body.get('by') or '匿名'} 标记开始",
        })
        return {"now": now, "records": store.list_execution(db)}

    raise ApiError(404, f"未知接口: {method} {path}")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, status, payload, content_type="application/json; charset=utf-8"):
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        elif isinstance(payload, str):
            payload = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _handle(self, method):
        try:
            from urllib.parse import urlparse, parse_qs
            u = urlparse(self.path)
            path = u.path
            query = {k: v[0] for k, v in parse_qs(u.query).items()}
            if path.startswith("/api/"):
                body = {}
                if method in ("POST", "PUT"):
                    length = int(self.headers.get("Content-Length") or 0)
                    raw = self.rfile.read(length) if length else b""
                    if raw:
                        body = json.loads(raw.decode("utf-8"))
                try:
                    result = handle_api(method, path, query, body)
                    self._send(200, result)
                except store.NotFound as e:
                    self._send(404, {"error": str(e)})
                except ApiError as e:
                    self._send(e.status, {"error": str(e), **e.extra})
                except json.JSONDecodeError:
                    self._send(400, {"error": "请求体不是合法 JSON"})
            else:
                self._serve_static(path)
        except BrokenPipeError:
            pass
        except Exception as e:  # 兜底，避免连接悬挂
            try:
                self._send(500, {"error": f"服务器内部错误: {e}"})
            except Exception:
                pass

    def _serve_static(self, path):
        if path in ("/", "/index.html"):
            path = "/index.html"
        safe = os.path.normpath(path).lstrip("/")
        full = os.path.join(STATIC, safe)
        if not full.startswith(STATIC) or not os.path.isfile(full):
            self._send(404, {"error": "not found"})
            return
        ext = os.path.splitext(full)[1]
        with open(full, "rb") as f:
            data = f.read()
        self._send(200, data, CONTENT_TYPES.get(ext, "application/octet-stream"))

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")


def main():
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", 8000))
    store.init_db()
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"项目组合分析系统: http://localhost:{port}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
