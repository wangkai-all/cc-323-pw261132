"""虚构示例数据：8 人、6 个项目、约 30 个阶段。

数据经过设计，使资源竞争真实影响结果：
- 「后端3」只有张莉、「数据3」只有赵敏、「前端3」只有陈晨 —— 高等级技能是瓶颈；
- p1/api 被 p3、p5 跨项目依赖，p2/svc 依赖 p4/warehouse —— 单项目最优排期
  无法直接拼成组合最优；
- 收益情景带概率分布，评估只能做事前期望，不能做事后评判。
"""

NOW = 6.0  # 当前周：W0–W6 已发生，W6 之后可调度

SKILLS = ["后端", "前端", "数据", "测试"]


def base_assumptions(strategy="critical_path"):
    return {
        "strategy": strategy,
        "horizon_weeks": 52,
        "switch_cost_weeks": 0.5,
        "people": [
            {"id": "u1", "name": "张莉", "cost_per_week": 1.6, "capacity": 1.0, "skills": {"后端": 3, "数据": 2}},
            {"id": "u2", "name": "王强", "cost_per_week": 1.2, "capacity": 1.0, "skills": {"后端": 2, "测试": 2}},
            {"id": "u3", "name": "陈晨", "cost_per_week": 1.4, "capacity": 1.0, "skills": {"前端": 3}},
            {"id": "u4", "name": "刘洋", "cost_per_week": 1.0, "capacity": 1.0, "skills": {"前端": 2, "后端": 1}},
            {"id": "u5", "name": "赵敏", "cost_per_week": 1.7, "capacity": 1.0, "skills": {"数据": 3, "后端": 2}},
            {"id": "u6", "name": "孙杰", "cost_per_week": 1.1, "capacity": 1.0, "skills": {"测试": 3, "前端": 1}},
            {"id": "u7", "name": "周婷", "cost_per_week": 1.3, "capacity": 1.0, "skills": {"后端": 2, "前端": 2}},
            {"id": "u8", "name": "吴凯", "cost_per_week": 1.0, "capacity": 1.0, "skills": {"数据": 2, "测试": 2}},
        ],
        "projects": [
            {
                "id": "p1", "name": "支付网关重构", "target_week": 40,
                "benefit_scenarios": [
                    {"prob": 0.25, "annual_benefit": 60},
                    {"prob": 0.5, "annual_benefit": 120},
                    {"prob": 0.25, "annual_benefit": 200},
                ],
                "phases": [
                    {"id": "req", "name": "需求分析", "skill": "后端", "min_level": 1, "effort_weeks": 3, "deps": []},
                    {"id": "arch", "name": "架构设计", "skill": "后端", "min_level": 3, "effort_weeks": 4, "deps": ["req"]},
                    {"id": "core", "name": "核心开发", "skill": "后端", "min_level": 2, "effort_weeks": 8, "deps": ["arch"]},
                    {"id": "api", "name": "开放接口", "skill": "后端", "min_level": 2, "effort_weeks": 4, "deps": ["arch"]},
                    {"id": "test", "name": "联调测试", "skill": "测试", "min_level": 2, "effort_weeks": 3, "deps": ["core", "api"]},
                ],
            },
            {
                "id": "p2", "name": "推荐系统", "target_week": 44,
                "benefit_scenarios": [
                    {"prob": 0.3, "annual_benefit": 40},
                    {"prob": 0.5, "annual_benefit": 90},
                    {"prob": 0.2, "annual_benefit": 180},
                ],
                "phases": [
                    {"id": "data", "name": "特征工程", "skill": "数据", "min_level": 3, "effort_weeks": 8, "deps": []},
                    {"id": "model", "name": "模型开发", "skill": "数据", "min_level": 3, "effort_weeks": 8, "deps": ["data"]},
                    {"id": "svc", "name": "在线服务", "skill": "后端", "min_level": 2, "effort_weeks": 5, "deps": ["model", "p4/warehouse"]},
                    {"id": "ab", "name": "AB实验", "skill": "数据", "min_level": 2, "effort_weeks": 3, "deps": ["svc"]},
                ],
            },
            {
                "id": "p3", "name": "移动App改版", "target_week": 36,
                "benefit_scenarios": [
                    {"prob": 0.3, "annual_benefit": 30},
                    {"prob": 0.5, "annual_benefit": 70},
                    {"prob": 0.2, "annual_benefit": 120},
                ],
                "phases": [
                    {"id": "proto", "name": "交互原型", "skill": "前端", "min_level": 3, "effort_weeks": 4, "deps": []},
                    {"id": "ui", "name": "UI开发", "skill": "前端", "min_level": 2, "effort_weeks": 7, "deps": ["proto"]},
                    {"id": "apic", "name": "接口联调", "skill": "后端", "min_level": 1, "effort_weeks": 3, "deps": ["ui", "p1/api"]},
                    {"id": "uat", "name": "验收测试", "skill": "测试", "min_level": 2, "effort_weeks": 2, "deps": ["apic"]},
                ],
            },
            {
                "id": "p4", "name": "数据平台", "target_week": 42,
                "benefit_scenarios": [
                    {"prob": 0.25, "annual_benefit": 50},
                    {"prob": 0.5, "annual_benefit": 100},
                    {"prob": 0.25, "annual_benefit": 150},
                ],
                "phases": [
                    {"id": "warehouse", "name": "数仓建设", "skill": "数据", "min_level": 3, "effort_weeks": 7, "deps": []},
                    {"id": "etl", "name": "ETL开发", "skill": "数据", "min_level": 2, "effort_weeks": 5, "deps": ["warehouse"]},
                    {"id": "bi", "name": "报表开发", "skill": "前端", "min_level": 1, "effort_weeks": 4, "deps": ["etl"]},
                ],
            },
            {
                "id": "p5", "name": "客服系统", "target_week": 46,
                "benefit_scenarios": [
                    {"prob": 0.4, "annual_benefit": 30},
                    {"prob": 0.4, "annual_benefit": 60},
                    {"prob": 0.2, "annual_benefit": 100},
                ],
                "phases": [
                    {"id": "flow", "name": "流程梳理", "skill": "后端", "min_level": 1, "effort_weeks": 2, "deps": []},
                    {"id": "dev", "name": "后台开发", "skill": "后端", "min_level": 3, "effort_weeks": 6, "deps": ["flow"]},
                    {"id": "int", "name": "接口集成", "skill": "后端", "min_level": 2, "effort_weeks": 3, "deps": ["dev", "p1/api"]},
                    {"id": "qa", "name": "测试上线", "skill": "测试", "min_level": 2, "effort_weeks": 2, "deps": ["int"]},
                ],
            },
            {
                "id": "p6", "name": "官网改版", "target_week": 30,
                "benefit_scenarios": [
                    {"prob": 0.5, "annual_benefit": 20},
                    {"prob": 0.5, "annual_benefit": 45},
                ],
                "phases": [
                    {"id": "design", "name": "页面设计", "skill": "前端", "min_level": 3, "effort_weeks": 3, "deps": []},
                    {"id": "build", "name": "页面开发", "skill": "前端", "min_level": 2, "effort_weeks": 4, "deps": ["design"]},
                    {"id": "qa", "name": "测试", "skill": "测试", "min_level": 1, "effort_weeks": 1, "deps": ["build"]},
                ],
            },
            {
                "id": "p7", "name": "风控引擎", "target_week": 46,
                "benefit_scenarios": [
                    {"prob": 0.3, "annual_benefit": 50},
                    {"prob": 0.5, "annual_benefit": 110},
                    {"prob": 0.2, "annual_benefit": 160},
                ],
                "phases": [
                    {"id": "rule", "name": "规则梳理", "skill": "数据", "min_level": 3, "effort_weeks": 5, "deps": []},
                    {"id": "eng", "name": "引擎开发", "skill": "后端", "min_level": 3, "effort_weeks": 7, "deps": ["rule"]},
                    {"id": "verify", "name": "验证上线", "skill": "测试", "min_level": 2, "effort_weeks": 3, "deps": ["eng"]},
                ],
            },
        ],
    }


def seed_execution():
    """已发生的执行事实（冻结记录）。now=6，以下阶段不可被新方案移动。"""
    return [
        {"phase_key": "p1/req", "project_id": "p1", "phase_id": "req", "person_id": "u7",
         "start_week": 1.0, "end_week": 4.0, "status": "done", "note": "需求分析已完成"},
        {"phase_key": "p1/arch", "project_id": "p1", "phase_id": "arch", "person_id": "u1",
         "start_week": 4.0, "end_week": 8.0, "status": "started", "note": "架构设计进行中（张莉）"},
        {"phase_key": "p3/proto", "project_id": "p3", "phase_id": "proto", "person_id": "u3",
         "start_week": 4.0, "end_week": 8.0, "status": "started", "note": "交互原型进行中（陈晨）"},
    ]
