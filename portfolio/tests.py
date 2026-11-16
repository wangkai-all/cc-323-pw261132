"""测试：调度不变量 + 并发/冻结语义 + API 冒烟。

运行：python3 tests.py
"""
import copy
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error

import scheduler
import seed
import store


def run(strategy="critical_path", assumptions=None, execution=None):
    a = assumptions or seed.base_assumptions(strategy)
    e = execution if execution is not None else seed.seed_execution()
    return scheduler.schedule(a, e, seed.NOW), a, e


class TestSchedulerInvariants(unittest.TestCase):
    def setUp(self):
        self.res, self.a, self.execution = run()

    def test_no_double_booking(self):
        """同一人员的占用区间不得重叠。"""
        by_person = {}
        for s in self.res["assignments"]:
            by_person.setdefault(s["person_id"], []).append((s["start"], s["end"]))
        for uid, ivs in by_person.items():
            ivs.sort()
            for (a1, b1), (a2, b2) in zip(ivs, ivs[1:]):
                self.assertLessEqual(b1, a2 + 1e-6, f"{uid} 区间重叠: {a1}-{b1} vs {a2}-{b2}")

    def test_dependencies_respected(self):
        """非冻结阶段的开工不早于所有依赖的完成时间。"""
        finish = {}
        for s in self.res["assignments"]:
            if s["kind"] == "work":
                finish[s["phase_key"]] = s["end"]
        starts = {}
        for s in self.res["assignments"]:
            if s["kind"] == "work":
                starts[s["phase_key"]] = s["start"]
        frozen = {e["phase_key"] for e in self.execution}
        for proj in self.a["projects"]:
            for ph in proj["phases"]:
                key = f"{proj['id']}/{ph['id']}"
                if key in frozen or key not in starts:
                    continue
                for d in ph.get("deps", []):
                    dk = d if "/" in d else f"{proj['id']}/{d}"
                    if dk in finish:
                        self.assertGreaterEqual(starts[key] + 1e-6, finish[dk],
                                                f"{key} 早于依赖 {dk} 完成开工")

    def test_frozen_pinned(self):
        """冻结阶段的位置与执行人与执行记录完全一致。"""
        for ex in self.execution:
            segs = [s for s in self.res["assignments"]
                    if s["phase_key"] == ex["phase_key"] and s["kind"] == "work"]
            self.assertEqual(len(segs), 1, f"冻结阶段 {ex['phase_key']} 应恰好出现一次")
            s = segs[0]
            self.assertTrue(s["frozen"])
            self.assertAlmostEqual(s["start"], ex["start_week"])
            self.assertAlmostEqual(s["end"], ex["end_week"])
            self.assertEqual(s["person_id"], ex["person_id"])

    def test_skills_respected(self):
        people = {p["id"]: p for p in self.a["people"]}
        phases = {f"{pr['id']}/{ph['id']}": ph for pr in self.a["projects"] for ph in pr["phases"]}
        for s in self.res["assignments"]:
            if s["kind"] != "work":
                continue
            ph = phases[s["phase_key"]]
            lvl = people[s["person_id"]]["skills"].get(ph["skill"], 0)
            self.assertGreaterEqual(lvl, ph["min_level"],
                                    f"{s['person_name']} 不满足 {s['phase_key']} 的技能要求")

    def test_nothing_scheduled_before_now(self):
        for s in self.res["assignments"]:
            if not s["frozen"]:
                self.assertGreaterEqual(s["start"], seed.NOW - 1e-6,
                                        f"非冻结段 {s['phase_key']} 排到了当前周之前")


class TestContentionMatters(unittest.TestCase):
    def test_removing_people_changes_result(self):
        """资源竞争必须实际影响结果：拿走人员后完成周应显著推迟。"""
        base, a, _ = run()
        fewer = copy.deepcopy(a)
        # 只留张莉一个后端（等级>=2 的活都压到她身上）
        for p in fewer["people"]:
            if p["id"] != "u1" and "后端" in p["skills"]:
                del p["skills"]["后端"]
        res2, _, _ = run(assumptions=fewer)
        f1 = base["projects"]["p1"]["finish"]
        f2 = res2["projects"]["p1"]["finish"]
        self.assertGreater(f2, f1 + 2, f"减员后 p1 完成周应明显推迟: {f1} -> {f2}")

    def test_strategies_differ(self):
        """两种策略应产生不同的排程结果。"""
        r1, _, _ = run("critical_path")
        r2, _, _ = run("expected_benefit")
        order1 = [(s["phase_key"], round(s["start"], 2)) for s in r1["assignments"] if s["kind"] == "work"]
        order2 = [(s["phase_key"], round(s["start"], 2)) for s in r2["assignments"] if s["kind"] == "work"]
        self.assertNotEqual(order1, order2, "两种策略的排程不应相同")

    def test_switch_cost_applied(self):
        """跨项目切换应产生切换段，且切换段占用人员时间。"""
        res, _, _ = run()
        switches = [s for s in res["assignments"] if s["kind"] == "switch"]
        self.assertTrue(len(switches) > 0, "示例数据应产生切换成本")
        for s in switches:
            self.assertAlmostEqual(s["end"] - s["start"], 0.5)


class TestFrozenViolations(unittest.TestCase):
    def test_editing_started_phase_flagged(self):
        """修改已开始阶段的工期 -> frozen_violations 非空。"""
        a = seed.base_assumptions()
        for proj in a["projects"]:
            for ph in proj["phases"]:
                if f"{proj['id']}/{ph['id']}" == "p1/arch":
                    ph["effort_weeks"] = 9  # 执行记录是 4 周
        res = scheduler.schedule(a, seed.seed_execution(), seed.NOW)
        self.assertTrue(any("p1/arch" in v for v in res["frozen_violations"]))

    def test_deleting_started_phase_flagged(self):
        a = seed.base_assumptions()
        for proj in a["projects"]:
            if proj["id"] == "p1":
                proj["phases"] = [p for p in proj["phases"] if p["id"] != "arch"]
                for p in proj["phases"]:
                    p["deps"] = [d for d in p.get("deps", []) if d != "arch"]
        res = scheduler.schedule(a, seed.seed_execution(), seed.NOW)
        self.assertTrue(any("p1/arch" in v for v in res["frozen_violations"]))


class TestStoreConcurrency(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = store.DB_PATH
        store.DB_PATH = os.path.join(self.tmp, "test.db")
        self.db = store.init_db()

    def tearDown(self):
        store.DB_PATH = self.old

    def test_version_conflict_on_save(self):
        """两个计划员各拿 v1 修改，后保存者必须被拒绝。"""
        s = store.get_scenario(self.db, 3)  # 草案
        a1 = copy.deepcopy(s["assumptions"]); a1["switch_cost_weeks"] = 1.0
        store.update_assumptions(self.db, 3, s["version"], a1, "计划员A")
        a2 = copy.deepcopy(s["assumptions"]); a2["switch_cost_weeks"] = 2.0
        with self.assertRaises(store.VersionConflict):
            store.update_assumptions(self.db, 3, s["version"], a2, "计划员B")

    def test_confirm_rejects_frozen_edit(self):
        s = store.get_scenario(self.db, 3)
        a = copy.deepcopy(s["assumptions"])
        for proj in a["projects"]:
            for ph in proj["phases"]:
                if f"{proj['id']}/{ph['id']}" == "p1/arch":
                    ph["effort_weeks"] = 9
        updated = store.update_assumptions(self.db, 3, s["version"], a, "计划员A")
        with self.assertRaises(store.StateError):
            store.confirm(self.db, 3, updated["version"], "计划员A")

    def test_confirm_version_conflict(self):
        s = store.get_scenario(self.db, 3)
        a = copy.deepcopy(s["assumptions"])
        store.update_assumptions(self.db, 3, s["version"], a, "计划员A")
        with self.assertRaises(store.VersionConflict):
            store.confirm(self.db, 3, s["version"], "计划员B")  # 旧版本号

    def test_confirm_ok_and_immutable(self):
        s = store.get_scenario(self.db, 3)
        confirmed = store.confirm(self.db, 3, s["version"], "计划员A")
        self.assertEqual(confirmed["status"], "confirmed")
        with self.assertRaises(store.StateError):
            store.update_assumptions(self.db, 3, confirmed["version"], confirmed["assumptions"], "计划员A")


class TestApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.old = store.DB_PATH
        store.DB_PATH = os.path.join(cls.tmp, "api.db")
        import server
        from http.server import ThreadingHTTPServer
        store.init_db()
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()
        time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        store.DB_PATH = cls.old

    def req(self, method, path, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(url, data=data, method=method,
                                   headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_bootstrap(self):
        code, data = self.req("GET", "/api/bootstrap")
        self.assertEqual(code, 200)
        self.assertEqual(len(data["scenarios"]), 3)
        self.assertEqual(len(data["execution"]), 3)

    def test_stale_confirm_409(self):
        _, s = self.req("GET", "/api/scenarios/3")
        # 先保存一次使版本前进
        _, s2 = self.req("PUT", "/api/scenarios/3", {"version": s["version"], "assumptions": s["assumptions"], "by": "t"})
        code, resp = self.req("POST", "/api/scenarios/3/confirm", {"version": s["version"], "by": "t"})
        self.assertEqual(code, 409)
        self.assertIn("current_version", resp)

    def test_frozen_edit_confirm_422(self):
        _, s = self.req("GET", "/api/scenarios/3")
        a = s["assumptions"]
        for proj in a["projects"]:
            for ph in proj["phases"]:
                if f"{proj['id']}/{ph['id']}" == "p3/proto":
                    ph["effort_weeks"] = 12
        _, s2 = self.req("PUT", "/api/scenarios/3", {"version": s["version"], "assumptions": a, "by": "t"})
        code, resp = self.req("POST", "/api/scenarios/3/confirm", {"version": s2["version"], "by": "t"})
        self.assertEqual(code, 422)
        self.assertIn("p3/proto", resp["error"])

    def test_compare_and_restore(self):
        code, rows = self.req("GET", "/api/compare?ids=1,2")
        self.assertEqual(code, 200)
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]["portfolio"], rows[1]["portfolio"])
        code, draft = self.req("POST", "/api/scenarios/1/restore", {"by": "t"})
        self.assertEqual(code, 200)
        self.assertEqual(draft["status"], "draft")
        self.assertIsNotNone(draft["schedule"])

    def test_execution_start_freezes_phase(self):
        _, s = self.req("GET", "/api/scenarios/3")
        code, res = self.req("POST", "/api/execution/start",
                             {"scenario_id": 3, "project_id": "p6", "phase_id": "design",
                              "person_id": "u3", "by": "t"})
        self.assertEqual(code, 200)
        keys = [r["phase_key"] for r in res["records"]]
        self.assertIn("p6/design", keys)
        # 现在修改 p6/design 的工期再确认，应被 422 拒绝
        _, s = self.req("GET", "/api/scenarios/3")
        a = s["assumptions"]
        for proj in a["projects"]:
            for ph in proj["phases"]:
                if f"{proj['id']}/{ph['id']}" == "p6/design":
                    ph["effort_weeks"] = 10
        _, s2 = self.req("PUT", "/api/scenarios/3", {"version": s["version"], "assumptions": a, "by": "t"})
        code, resp = self.req("POST", "/api/scenarios/3/confirm", {"version": s2["version"], "by": "t"})
        self.assertEqual(code, 422)


if __name__ == "__main__":
    unittest.main(verbosity=2)
