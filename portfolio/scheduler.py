"""组合调度引擎：资源受限的项目组合排程（RCPSP 风格的串行调度）。

核心约定：
- 时间单位为"周"（float），时间轴从 W0 开始；now 表示"当前周"，
  now 之后的时间才可调度，now 之前/跨 now 的冻结记录不可移动。
- 每个人同一时刻只能做一个任务；跨项目切换产生 switch_cost 周的占用
  （切换期间该人员被占用但不产出该阶段进度）。
- 已开始/已完成的阶段（execution 冻结记录）位置与执行人固定，
  新方案不得悄悄移动；若方案假设与执行记录冲突，记入 frozen_violations，
  确认方案时会被拒绝。
- 所有评估均为"事前"（ex-ante）：只用方案假设中的概率与收益分布，
  不使用任何事后实际值。
"""
from __future__ import annotations

import random
from collections import defaultdict

EPS = 1e-6
STRATEGIES = ("critical_path", "expected_benefit")
WEEKS_PER_YEAR = 52.0


def phase_key(project_id: str, phase_id: str) -> str:
    return f"{project_id}/{phase_id}"


def expected_annual_benefit(project: dict) -> float:
    """项目年化收益的事前期望（由收益情景概率加权）。"""
    return sum(s["prob"] * s["annual_benefit"] for s in project["benefit_scenarios"])


def _find_placement(intervals, est, duration, switch_cost, project_id):
    """在某人员已占用的区间列表中，为时长 duration 的任务找最早可放置位置。

    返回 dict(work_start, work_end, gap_start, sw, prev, next)。
    切换成本 sw：若紧邻的前一段占用属于其他项目，则开工前需 sw 周切换。
    """
    best = None
    n = len(intervals)
    for i in range(n + 1):
        prev = intervals[i - 1] if i > 0 else None
        nxt = intervals[i] if i < n else None
        gap_start = max(est, prev["end"] if prev else est)
        sw = switch_cost if (prev and prev["project_id"] != project_id) else 0.0
        work_start = gap_start + sw
        block_end = work_start + duration
        if nxt and block_end > nxt["start"] + EPS:
            continue
        cand = {
            "work_start": work_start,
            "work_end": block_end,
            "gap_start": gap_start,
            "sw": sw,
            "prev": prev,
        }
        if best is None or (work_start, block_end) < (best["work_start"], best["work_end"]):
            best = cand
    return best


def schedule(assumptions: dict, execution: list, now: float) -> dict:
    """对一组假设执行排程，返回排程结果与事前评估指标。"""
    people = {p["id"]: p for p in assumptions["people"]}
    projects = {p["id"]: p for p in assumptions["projects"]}
    horizon_weeks = float(assumptions.get("horizon_weeks", 52))
    horizon_end = now + horizon_weeks
    switch_cost = float(assumptions.get("switch_cost_weeks", 0.5))
    strategy = assumptions.get("strategy", "critical_path")

    # ---- 展开阶段、规范化依赖（项目内写阶段 id，跨项目写 "proj/phase"）----
    phases = {}
    for proj in assumptions["projects"]:
        for ph in proj["phases"]:
            key = phase_key(proj["id"], ph["id"])
            deps = [d if "/" in d else phase_key(proj["id"], d) for d in ph.get("deps", [])]
            phases[key] = {**ph, "key": key, "project_id": proj["id"], "deps": deps}

    warnings = []

    # ---- 后继图、关键链长度、下游受益项目 ----
    succ = defaultdict(list)
    for k, ph in phases.items():
        for d in ph["deps"]:
            if d not in phases:
                warnings.append(f"阶段 {k} 依赖了不存在的阶段 {d}，该依赖被忽略")
                continue
            succ[d].append(k)

    chain = {}
    _visiting = set()

    def chain_len(k):
        if k in chain:
            return chain[k]
        if k in _visiting:
            return phases[k]["effort_weeks"]
        _visiting.add(k)
        best = 0.0
        for s in succ.get(k, []):
            best = max(best, chain_len(s))
        _visiting.discard(k)
        chain[k] = float(phases[k]["effort_weeks"]) + best
        return chain[k]

    down_cache = {}

    def downstream_projects(k):
        if k in down_cache:
            return down_cache[k]
        res = set()
        for s in succ.get(k, []):
            res.add(phases[s]["project_id"])
            res |= downstream_projects(s)
        res.discard(phases[k]["project_id"])
        down_cache[k] = res
        return res

    for k in phases:
        chain_len(k)
        downstream_projects(k)

    exp_ben = {pid: expected_annual_benefit(p) for pid, p in projects.items()}

    def priority(k):
        """返回值越小越先排。两种策略在此分岔。"""
        ph = phases[k]
        proj = projects[ph["project_id"]]
        c = chain[k]
        if strategy == "expected_benefit":
            # 收益密度：本项目的期望年化收益 + 被它卡住的所有下游项目的期望收益，除以关键链长度
            b = exp_ben[ph["project_id"]] + sum(exp_ben[d] for d in downstream_projects(k))
            return (-(b / max(c, EPS)), k)
        # critical_path：最晚可开工时间（目标完成周 - 关键链长度），余量最小者优先
        target = proj.get("target_week") or horizon_end
        return (target - c, k)

    # ---- 人员占用区间 ----
    intervals = {uid: [] for uid in people}
    assignments = []

    def book(uid, seg):
        intervals[uid].append(seg)
        intervals[uid].sort(key=lambda s: (s["start"], s["end"]))
        assignments.append(seg)

    finish = {}
    frozen_violations = []

    # ---- 冻结已开始/已完成的阶段：位置、执行人均不可动 ----
    for ex in execution:
        key = ex["phase_key"]
        label = "已开始" if ex["status"] == "started" else "已完成"
        if key not in phases:
            frozen_violations.append(f"方案中删除了{label}的阶段 {key}，不能确认")
            continue
        ph = phases[key]
        uid = ex["person_id"]
        if uid not in people:
            frozen_violations.append(f"{label}阶段 {key} 的执行人 {uid} 不在方案人员列表中")
            continue
        person = people[uid]
        cap = float(person.get("capacity", 1.0) or 1.0)
        expected_len = float(ph["effort_weeks"]) / cap
        actual_len = float(ex["end_week"]) - float(ex["start_week"])
        if abs(expected_len - actual_len) > 0.01:
            frozen_violations.append(
                f"{label}阶段 {key} 的工期被修改：执行记录 {actual_len:.1f} 周，方案假设 {expected_len:.1f} 周"
            )
        lvl = person.get("skills", {}).get(ph["skill"], 0)
        if lvl < ph.get("min_level", 1):
            frozen_violations.append(
                f"{label}阶段 {key} 的技能要求被改为 {ph['skill']}≥{ph.get('min_level', 1)}，"
                f"执行人 {person['name']} 不再满足"
            )
        proj = projects[ph["project_id"]]
        seg = {
            "kind": "work",
            "frozen": True,
            "phase_key": key,
            "phase_id": ph["id"],
            "phase_name": ph["name"],
            "project_id": ph["project_id"],
            "project_name": proj["name"],
            "person_id": uid,
            "person_name": person["name"],
            "skill": ph["skill"],
            "start": float(ex["start_week"]),
            "end": float(ex["end_week"]),
            "switch_weeks": 0.0,
            "reasons": [f"已冻结（{label}，执行记录 W{ex['start_week']:g}–W{ex['end_week']:g}），新方案不得移动"],
        }
        book(uid, seg)
        finish[key] = float(ex["end_week"])

    # ---- 串行调度：每轮从就绪集合中按策略优先级取一个阶段 ----
    waits = []
    unscheduled = [k for k in phases if k not in finish]
    guard = 0
    while unscheduled:
        guard += 1
        if guard > 100000:
            warnings.append("调度循环异常终止")
            break
        ready = [k for k in unscheduled if all(d in finish for d in phases[k]["deps"] if d in phases)]
        if not ready:
            warnings.append("依赖存在循环，以下阶段无法排程: " + ", ".join(sorted(unscheduled)))
            break
        ready.sort(key=priority)
        k = ready[0]
        ph = phases[k]
        proj = projects[ph["project_id"]]
        dep_finish = max([finish[d] for d in ph["deps"] if d in finish] + [now])
        est = max(now, dep_finish)

        cands = []
        for uid, person in people.items():
            lvl = person.get("skills", {}).get(ph["skill"], 0)
            if lvl < ph.get("min_level", 1):
                continue
            cap = float(person.get("capacity", 1.0) or 1.0)
            dur = float(ph["effort_weeks"]) / cap
            place = _find_placement(intervals[uid], est, dur, switch_cost, ph["project_id"])
            cands.append((place["work_end"], float(person.get("cost_per_week", 1.0)), uid, place, dur))
        if not cands:
            warnings.append(f"阶段 {k} 无人满足技能 {ph['skill']}≥{ph.get('min_level', 1)}，无法排程")
            finish[k] = est  # 避免阻塞下游，但结果已失真并在 warnings 中明示
            unscheduled.remove(k)
            continue
        cands.sort(key=lambda t: (t[0], t[1], t[2]))
        work_end, _cost, uid, place, dur = cands[0]
        person = people[uid]

        reasons = []
        if dep_finish > now + EPS:
            dep_names = [d for d in ph["deps"] if d in finish and abs(finish[d] - dep_finish) < EPS]
            reasons.append(f"等待依赖完成（{', '.join(dep_names)}，W{dep_finish:.1f}）")
        if place["sw"] > EPS:
            prev_proj = projects[place["prev"]["project_id"]]["name"]
            reasons.append(f"切换成本 {place['sw']:.1f} 周：{prev_proj} → {proj['name']}")
        if place["gap_start"] > est + EPS and place["prev"]:
            pv = place["prev"]
            reasons.append(
                f"等待资源：{person['name']} 在 W{pv['start']:.1f}–W{pv['end']:.1f} "
                f"被「{pv['project_name']}/{pv['phase_name']}」占用"
            )
            waits.append({
                "phase_key": k,
                "phase_name": ph["name"],
                "project_name": proj["name"],
                "person_id": uid,
                "person_name": person["name"],
                "weeks": round(place["gap_start"] - est, 2),
                "blocker": {
                    "phase_key": pv["phase_key"],
                    "phase_name": pv["phase_name"],
                    "project_name": pv["project_name"],
                    "start": pv["start"],
                    "end": pv["end"],
                },
            })
        if not reasons:
            reasons.append("依赖就绪、资源空闲，立即开始")

        if place["sw"] > EPS:
            book(uid, {
                "kind": "switch",
                "frozen": False,
                "phase_key": k + "#switch",
                "phase_id": ph["id"],
                "phase_name": ph["name"] + "（切换）",
                "project_id": ph["project_id"],
                "project_name": proj["name"],
                "person_id": uid,
                "person_name": person["name"],
                "skill": None,
                "start": place["gap_start"],
                "end": place["work_start"],
                "switch_weeks": place["sw"],
                "reasons": [f"从「{projects[place['prev']['project_id']]['name']}」切换到「{proj['name']}」的熟悉成本"],
            })
        book(uid, {
            "kind": "work",
            "frozen": False,
            "phase_key": k,
            "phase_id": ph["id"],
            "phase_name": ph["name"],
            "project_id": ph["project_id"],
            "project_name": proj["name"],
            "person_id": uid,
            "person_name": person["name"],
            "skill": ph["skill"],
            "start": place["work_start"],
            "end": work_end,
            "switch_weeks": place["sw"],
            "reasons": reasons,
        })
        finish[k] = work_end
        unscheduled.remove(k)

    # ---- 指标（全部为事前评估）----
    labor_cost = 0.0
    for s in assignments:
        labor_cost += (s["end"] - s["start"]) * float(people[s["person_id"]].get("cost_per_week", 1.0))

    proj_metrics = {}
    for pid, proj in projects.items():
        keys = [k for k in phases if phases[k]["project_id"] == pid]
        fin = max((finish[k] for k in keys), default=None)
        eb = exp_ben[pid]
        realized = eb * max(0.0, horizon_end - fin) / WEEKS_PER_YEAR if fin is not None else 0.0
        target = proj.get("target_week")
        proj_metrics[pid] = {
            "name": proj["name"],
            "finish": round(fin, 2) if fin is not None else None,
            "target_week": target,
            "late": round(max(0.0, fin - target), 2) if (fin is not None and target) else 0.0,
            "expected_annual": round(eb, 2),
            "realized_expected": round(realized, 2),
        }

    expected_benefit_total = sum(m["realized_expected"] for m in proj_metrics.values())

    # 蒙特卡洛：按各项目收益情景独立抽样（固定种子，结果可复现）
    rng = random.Random(42)
    n_sim = 2000
    sim_values = []
    sampled_projects = [
        (pid, proj_metrics[pid]["finish"], proj["benefit_scenarios"])
        for pid, proj in projects.items()
        if proj_metrics[pid]["finish"] is not None
    ]
    for _ in range(n_sim):
        total = -labor_cost
        for _pid, fin, scenarios in sampled_projects:
            r = rng.random()
            acc = 0.0
            sampled = scenarios[-1]["annual_benefit"]
            for s in scenarios:
                acc += s["prob"]
                if r <= acc:
                    sampled = s["annual_benefit"]
                    break
            total += sampled * max(0.0, horizon_end - fin) / WEEKS_PER_YEAR
        sim_values.append(total)
    sim_values.sort()

    def pct(q):
        return round(sim_values[min(n_sim - 1, int(q * n_sim))], 2)

    prob_negative = round(sum(1 for v in sim_values if v < 0) / n_sim, 4)

    people_stats = {}
    for uid, person in people.items():
        busy = 0.0
        sw_total = 0.0
        for s in intervals[uid]:
            a, b = max(s["start"], now), min(s["end"], horizon_end)
            if b > a:
                busy += b - a
            if s["kind"] == "switch":
                sw_total += s["end"] - s["start"]
        people_stats[uid] = {
            "name": person["name"],
            "busy_weeks": round(busy, 2),
            "utilization": round(busy / horizon_weeks, 3) if horizon_weeks else 0.0,
            "switch_weeks": round(sw_total, 2),
        }

    skills_stats = {}
    for ph in phases.values():
        st = skills_stats.setdefault(ph["skill"], {"demand_weeks": 0.0, "busy_weeks": 0.0, "bands": []})
        st["demand_weeks"] += float(ph["effort_weeks"])
    for s in assignments:
        if s["kind"] == "work" and s["skill"]:
            skills_stats[s["skill"]]["busy_weeks"] += s["end"] - s["start"]
    for skill, st in skills_stats.items():
        # 累计等级带：等级≥L 的需求只能由等级≥L 的人承担。
        # 瓶颈往往只在最高等级带出现（如「数据≥3 只有赵敏」），整体负荷率会掩盖它。
        levels = sorted({ph.get("min_level", 1) for ph in phases.values() if ph["skill"] == skill})
        for L in levels:
            demand = sum(float(ph["effort_weeks"]) for ph in phases.values()
                         if ph["skill"] == skill and ph.get("min_level", 1) >= L)
            qual = [p for p in people.values() if p.get("skills", {}).get(skill, 0) >= L]
            cap_weeks = sum(float(p.get("capacity", 1.0) or 1.0) for p in qual) * horizon_weeks
            st["bands"].append({
                "min_level": L,
                "demand_weeks": round(demand, 2),
                "capacity_weeks": round(cap_weeks, 2),
                "load_ratio": round(demand / cap_weeks, 3) if cap_weeks else None,
                "qualified": [p["name"] for p in qual],
            })
        st["demand_weeks"] = round(st["demand_weeks"], 2)
        st["busy_weeks"] = round(st["busy_weeks"], 2)
        worst = max(st["bands"], key=lambda b: b["load_ratio"] or 0)
        st["load_ratio"] = worst["load_ratio"]

    return {
        "now": now,
        "horizon_end": horizon_end,
        "strategy": strategy,
        "assignments": sorted(assignments, key=lambda s: (s["person_id"], s["start"])),
        "projects": proj_metrics,
        "portfolio": {
            "expected_benefit": round(expected_benefit_total, 2),
            "labor_cost": round(labor_cost, 2),
            "net_expected": round(expected_benefit_total - labor_cost, 2),
            "p10": pct(0.10),
            "p50": pct(0.50),
            "p90": pct(0.90),
            "prob_negative": prob_negative,
            "switch_weeks_total": round(sum(s["end"] - s["start"] for s in assignments if s["kind"] == "switch"), 2),
        },
        "people": people_stats,
        "skills": skills_stats,
        "waits": waits,
        "warnings": warnings,
        "frozen_violations": frozen_violations,
    }
