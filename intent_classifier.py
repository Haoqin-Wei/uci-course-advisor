# router/intent_classifier.py
#
# 职责：判断用户问题是否与选课相关，并进一步分类子意图。
# 这是 Router 层的第一步，所有问题都先经过这里。
#
# 依赖：
#   - config/routing_rules.py  （业务规则配置，由开发者填写）
#   - state/session_state.py   （可选：多轮上下文参考）
# ─────────────────────────────────────────────────────────────────────────────

from typing import Optional
from config.routing_rules import (
    COURSE_RELATED_KEYWORDS,
    COURSE_UNRELATED_PATTERNS,
    INTENT_CLASSIFICATION_RULES,
    COURSE_INTENT_TYPES,
)
from state.session_state import SessionState


# ─────────────────────────────────────────────────────────────────────────────
# 函数 1：is_course_related
# ─────────────────────────────────────────────────────────────────────────────

def is_course_related(question: str, state: Optional[SessionState] = None) -> bool:
    """
    判断用户输入的问题是否属于"选课相关"范畴。

    这是 Router 的第一道门：只有返回 True，问题才会继续进入选课 pipeline。

    Args:
        question (str): 用户当前输入的原始文本。
        state (SessionState, optional): 当前会话状态，用于多轮上下文参考。
            - 例如：上一轮已经在讨论某门课，本轮即使没有明显关键词也可能是选课相关。
            - TODO: 由开发者决定 state 是否参与此判断，以及如何参与。

    Returns:
        bool:
            True  → 问题属于选课相关，继续进入选课 pipeline
            False → 问题与选课无关，交由其他 handler 处理（如通用对话）

    判断策略（TODO: 由开发者选择并实现）：
        可选方案 A：关键词匹配（基于 COURSE_RELATED_KEYWORDS）
        可选方案 B：规则 + 排除模式（结合 COURSE_UNRELATED_PATTERNS）
        可选方案 C：调用 LLM 做语义分类
        可选方案 D：混合策略（规则优先，LLM 兜底）
        → 请在 config/routing_rules.py 中定义规则，在此处实现调用逻辑
    """

    # TODO: 由开发者实现 —— 根据 COURSE_RELATED_KEYWORDS 和
    #       COURSE_UNRELATED_PATTERNS 判断 question 是否选课相关。
    #
    # 示例结构（不含实际逻辑）:
    #
    #   step 1: 检查排除规则（如命中排除模式，直接返回 False）
    #   step 2: 检查正向关键词（如命中关键词，返回 True）
    #   step 3: 多轮上下文补充判断（如 state 中有相关上下文，可返回 True）
    #   step 4: 兜底返回 False

    raise NotImplementedError(
        "is_course_related() 尚未实现。"
        "请先在 config/routing_rules.py 中填写规则，再在此处实现判断逻辑。"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 函数 2：classify_course_intent
# ─────────────────────────────────────────────────────────────────────────────

def classify_course_intent(question: str, state: Optional[SessionState] = None) -> str:
    """
    在确认问题选课相关后，进一步分类其子意图（intent）。

    此函数仅在 is_course_related() 返回 True 后调用。

    Args:
        question (str): 用户当前输入的原始文本。
        state (SessionState, optional): 当前会话状态，用于多轮上下文参考。
            - TODO: 由开发者决定 state 是否参与此分类，以及如何参与。

    Returns:
        str: 子意图类型字符串，取值来自 config/routing_rules.py 中的
             COURSE_INTENT_TYPES 列表。
             若无法分类，返回 "unknown"。

    支持的子意图（TODO: 由开发者在 config/routing_rules.py 中定义）：
        例如:
            "prerequisite_check"     —— 先修课查询
            "schedule_conflict"      —— 时间冲突 / 排课建议
            "professor_review"       —— 教授口碑 / RMP 评分
            "grade_distribution"     —— 历史给分率（Zotistics）
            "major_restriction"      —— 专业 / 学院限制
            "general_recommendation" —— 泛化推荐请求
            "unknown"                —— 无法归类

    分类策略（TODO: 由开发者选择并实现）：
        可选方案 A：基于 INTENT_CLASSIFICATION_RULES 的关键词匹配
        可选方案 B：调用 LLM 做 intent classification（zero-shot 或 few-shot）
        可选方案 C：混合策略
        → 请在 config/routing_rules.py 中定义规则，在此处实现调用逻辑
    """

    # TODO: 由开发者实现 —— 根据 INTENT_CLASSIFICATION_RULES 对 question 分类。
    #
    # 示例结构（不含实际逻辑）:
    #
    #   for each intent_type in INTENT_CLASSIFICATION_RULES:
    #       if question matches rules for intent_type:
    #           return intent_type
    #   return "unknown"

    raise NotImplementedError(
        "classify_course_intent() 尚未实现。"
        "请先在 config/routing_rules.py 中填写 INTENT_CLASSIFICATION_RULES，"
        "再在此处实现分类逻辑。"
    )
