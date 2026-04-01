# UCI Course Recommendation Assistant — Demo Skeleton

## 当前阶段目标
最小可运行 demo，验证 Router → Skill Routing → Handler 的整体 workflow。

## 项目目录结构

```
uci-course-advisor/
│
├── main.py                        # 入口：接收用户输入，调用 Router
│
├── router/
│   ├── __init__.py
│   ├── intent_classifier.py       # 判断问题是否选课相关 & 子意图分类
│   ├── skill_gate.py              # 判断是否应调用 course skill
│   └── dispatcher.py             # 根据 intent 分发到对应 handler
│
├── skills/
│   ├── __init__.py
│   └── course_skill.py            # Course Recommendation Skill 入口（stub）
│
├── handlers/
│   ├── __init__.py
│   ├── prerequisite_handler.py    # 处理先修课相关问题（stub）
│   ├── schedule_handler.py        # 处理时间冲突 / 排课问题（stub）
│   ├── professor_handler.py       # 处理教授口碑相关问题（stub）
│   ├── grade_handler.py           # 处理历史给分率相关问题（stub）
│   └── general_handler.py        # 处理无法分类的选课问题（stub）
│
├── state/
│   ├── __init__.py
│   └── session_state.py           # 会话状态数据结构定义
│
├── config/
│   └── routing_rules.py           # ⚠️ 业务规则占位文件（开发者填写）
│
└── tests/
    └── test_router.py             # Router 单元测试桩
```

## 各文件职责说明

| 文件 | 职责 |
|------|------|
| `main.py` | 接收用户输入，驱动整个 routing pipeline |
| `router/intent_classifier.py` | `is_course_related()` + `classify_course_intent()` |
| `router/skill_gate.py` | `should_invoke_course_skill()` |
| `router/dispatcher.py` | `route_course_request()` → 指向具体 handler |
| `skills/course_skill.py` | Course Skill 的统一入口，内部再细分 |
| `handlers/*.py` | 各子意图的具体处理逻辑（本阶段均为 stub） |
| `state/session_state.py` | 会话上下文（专业、学院、已选课等） |
| `config/routing_rules.py` | 所有业务判断规则的集中配置入口 |
| `tests/test_router.py` | 用于验证 routing 逻辑是否符合预期 |

## 开发顺序建议
1. 先填写 `config/routing_rules.py` 中的规则
2. 再实现 `router/intent_classifier.py` 中的判断逻辑
3. 然后实现 `router/skill_gate.py`
4. 最后实现 `router/dispatcher.py`
5. Handler 层可以先返回 mock 数据验证通路
