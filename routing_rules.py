# config/routing_rules.py
#
# ⚠️ 这是业务规则的集中配置入口。
# 所有判断逻辑的"依据"应该在这里定义，而不是散落在各个函数里。
# 本文件中的所有内容均需开发者根据实际业务填写。
# ─────────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
# 规则 1：什么关键词 / 语义特征 代表"选课相关"问题？
# 用途：is_course_related() 函数的判断依据
# 填写方式：可以是关键词列表、正则、语义标签等（你来决定策略）
# ─────────────────────────────────────────────────────────────────────────────

# TODO: 由开发者填写 —— 哪些词或模式代表用户在问选课相关问题？
# 示例占位（不代表实际规则，请替换）:
COURSE_RELATED_KEYWORDS: list[str] = [
    # TODO: 填写触发选课意图的关键词
    # 例如: "course", "class", "enroll", "prerequisite", "professor", ...
]

# TODO: 由开发者填写 —— 哪些模式代表用户「不是」在问选课问题（排除规则）？
COURSE_UNRELATED_PATTERNS: list[str] = [
    # TODO: 填写需要排除的模式
    # 例如: 纯闲聊、与课程无关的校园问题、天气等
]


# ─────────────────────────────────────────────────────────────────────────────
# 规则 2：选课子意图分类（Intent Taxonomy）
# 用途：classify_course_intent() 函数的分类依据
# ─────────────────────────────────────────────────────────────────────────────

# TODO: 由开发者填写 —— 定义所有支持的选课子意图类型
# 以下为结构占位，请根据实际需求增删改
COURSE_INTENT_TYPES: list[str] = [
    # TODO: 填写子意图类型，例如:
    # "prerequisite_check"   —— 询问某课是否有先修要求
    # "schedule_conflict"    —— 询问时间冲突 / 排课建议
    # "professor_review"     —— 询问教授口碑 / RMP 评分
    # "grade_distribution"   —— 询问历史给分率 (Zotistics)
    # "major_restriction"    —— 询问专业 / 学院限制
    # "general_recommendation" —— 泛化的选课推荐请求
    # "unknown"              —— 无法分类
]

# TODO: 由开发者填写 —— 每个子意图对应哪些触发特征？
# 格式建议: { intent_type: [keywords / patterns] }
INTENT_CLASSIFICATION_RULES: dict[str, list[str]] = {
    # TODO: 填写每种 intent 的触发规则
    # 例如:
    # "prerequisite_check": ["prerequisite", "prereq", "required before", ...],
    # "professor_review":   ["professor", "instructor", "RMP", "rating", ...],
}


# ─────────────────────────────────────────────────────────────────────────────
# 规则 3：什么情况下调用 Course Skill？什么情况下不调用？
# 用途：should_invoke_course_skill() 函数的判断依据
# ─────────────────────────────────────────────────────────────────────────────

# TODO: 由开发者填写 —— 满足什么条件才调用 skill（而不是直接回复或走其他流程）？
# 例如: 需要外部数据查询、需要多步骤推理、需要个性化推荐时
SKILL_INVOCATION_CONDITIONS: list[str] = [
    # TODO: 填写触发 skill 调用的条件描述（用于文档和逻辑实现）
]

# TODO: 由开发者填写 —— 什么情况下「不」调用 skill？
# 例如: 问题过于模糊、用户状态不完整、属于纯 FAQ 可以直接回答
SKILL_SKIP_CONDITIONS: list[str] = [
    # TODO: 填写跳过 skill 调用的条件描述
]


# ─────────────────────────────────────────────────────────────────────────────
# 规则 4：每种 intent → 路由到哪个 handler？
# 用途：route_course_request() 函数的路由表
# ─────────────────────────────────────────────────────────────────────────────

# TODO: 由开发者填写 —— intent 到 handler 的映射关系
# 格式: { intent_type: handler_module_or_function_name }
INTENT_TO_HANDLER_MAP: dict[str, str] = {
    # TODO: 填写路由映射
    # 例如:
    # "prerequisite_check":    "handlers.prerequisite_handler",
    # "schedule_conflict":     "handlers.schedule_handler",
    # "professor_review":      "handlers.professor_handler",
    # "grade_distribution":    "handlers.grade_handler",
    # "general_recommendation":"handlers.general_handler",
    # "unknown":               "handlers.general_handler",
}
