# skills/course_skill.py
#
# 职责：Course Recommendation Skill 的统一入口。
# Router 决定调用 skill 后，所有请求都经过这里，再由此分发到具体 handler。
#
# 当前阶段：stub，所有处理逻辑均为占位。
# ─────────────────────────────────────────────────────────────────────────────

from typing import Any
from state.session_state import SessionState
from router.dispatcher import route_course_request


def invoke_course_skill(
    intent: str,
    question: str,
    state: SessionState,
) -> dict[str, Any]:
    """
    Course Recommendation Skill 的主入口函数。

    Args:
        intent (str): 由 Router 分类出的子意图。
        question (str): 用户原始输入。
        state (SessionState): 当前会话状态。

    Returns:
        dict: handler 处理结果（结构见 dispatcher.py）

    TODO: 由开发者决定 skill 层是否需要做以下事情（或直接透传到 dispatcher）：
        - 调用前的参数校验 / 补全
        - 调用前的权限检查（例如：用户是否已提供足够的 profile 信息）
        - 调用后的结果后处理（例如：格式化、过滤、排序）
        - 调用失败时的重试 / 降级策略
    """

    # TODO: 由开发者决定是否在这里做参数预处理
    # 例如: 检查 state.user_profile.major 是否为空，如空则提示用户补充

    # 透传到 dispatcher
    result = route_course_request(intent=intent, question=question, state=state)

    # TODO: 由开发者决定是否在这里做结果后处理
    # 例如: 对推荐结果排序、过滤不符合时间偏好的课程等

    # 标记 skill 已调用
    state.skill_invoked = True

    return result
