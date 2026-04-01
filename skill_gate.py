# router/skill_gate.py
#
# 职责：在确定问题属于选课相关后，决定是否需要调用 Course Recommendation Skill。
#
# 背景：
#   并非所有选课相关问题都需要调用 skill。
#   例如，"CS 161 的先修课是什么？" 可能只需要查一条规则；
#   而 "帮我根据我的专业和时间偏好推荐课程" 则需要完整的 skill pipeline。
#   这一层负责做这个区分。
#
# 依赖：
#   - config/routing_rules.py（业务规则配置）
#   - state/session_state.py（会话状态）
# ─────────────────────────────────────────────────────────────────────────────

from typing import Optional
from config.routing_rules import (
    SKILL_INVOCATION_CONDITIONS,
    SKILL_SKIP_CONDITIONS,
)
from state.session_state import SessionState


# ─────────────────────────────────────────────────────────────────────────────
# 函数 3：should_invoke_course_skill
# ─────────────────────────────────────────────────────────────────────────────

def should_invoke_course_skill(question: str, state: SessionState) -> bool:
    """
    在确认问题为选课相关后，判断是否需要调用 Course Recommendation Skill。

    调用 skill 意味着启动一个更重的处理流程（可能包括外部 API 查询、
    多步推理、个性化推荐等）。本函数决定是否值得走这条路径。

    Args:
        question (str): 用户当前输入的原始文本。
        state (SessionState): 当前会话状态，包含用户画像和对话历史。
            - 重要：state 中的信息（如 major、completed_courses）可能直接
              影响是否需要调用 skill。
            - TODO: 由开发者决定哪些 state 字段是 skill 调用决策的必要条件。

    Returns:
        bool:
            True  → 需要调用 Course Skill，走完整 skill pipeline
            False → 不需要调用 skill，可以直接回复或走轻量级 handler

    决策依据（TODO: 由开发者在 config/routing_rules.py 中填写并在此实现）：

        应该调用 skill 的情况（参考 SKILL_INVOCATION_CONDITIONS）：
            TODO: 由开发者填写，例如：
            - 用户请求个性化推荐（需要结合 profile）
            - 问题涉及多个数据源（RMP + Zotistics + WebSoc）
            - 需要多步骤规划（排课 + 先修检查 + 时间冲突）
            - ...

        不应该调用 skill 的情况（参考 SKILL_SKIP_CONDITIONS）：
            TODO: 由开发者填写，例如：
            - 问题过于模糊，缺少必要上下文
            - 用户 profile 信息不完整，无法个性化
            - 属于简单 FAQ，可以直接静态回答
            - 已经调用过 skill（state.skill_invoked == True）
            - ...

    注意事项：
        - 此函数不负责"能不能调用"，只负责"该不该调用"。
        - 权限 / 限流等问题由其他模块处理。
    """

    # 防止重复调用（示例逻辑，可按需调整）
    if state.skill_invoked:
        # TODO: 由开发者决定：如果本轮已经调用过 skill，是否允许再次调用？
        return False

    # TODO: 由开发者实现 —— 根据 SKILL_INVOCATION_CONDITIONS 和
    #       SKILL_SKIP_CONDITIONS 以及 state 综合判断是否调用 skill。
    #
    # 示例结构（不含实际逻辑）:
    #
    #   step 1: 检查 skip 条件（如命中任意 skip 条件，返回 False）
    #   step 2: 检查 invocation 条件（如命中任意 invoke 条件，返回 True）
    #   step 3: 检查 state 完整性（如缺少必要字段，返回 False 并提示用户补充）
    #   step 4: 兜底返回 False（保守策略：不确定时不调用）

    raise NotImplementedError(
        "should_invoke_course_skill() 尚未实现。"
        "请先在 config/routing_rules.py 中填写 SKILL_INVOCATION_CONDITIONS "
        "和 SKILL_SKIP_CONDITIONS，再在此处实现判断逻辑。"
    )
