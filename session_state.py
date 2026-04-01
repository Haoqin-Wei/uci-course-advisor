# state/session_state.py
#
# 会话状态数据结构定义。
# Router 和 Skill 在做决策时可以读取 state，但不应直接修改 state。
# state 的更新由 main.py 或专门的 state manager 统一管理。
# ─────────────────────────────────────────────────────────────────────────────

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class UserProfile:
    """
    用户基本信息（由对话收集，非系统账号信息）。

    TODO: 由开发者决定哪些字段是 routing 决策所必需的。
    以下字段仅为结构占位，按需增删。
    """

    major: Optional[str] = None
    # TODO: 是否需要区分 major vs minor？
    # TODO: school/college 字段是否用于 skill 路由判断？

    school: Optional[str] = None
    # 例如: "Donald Bren School of ICS", "School of Engineering"

    year: Optional[str] = None
    # 例如: "freshman", "sophomore", "junior", "senior", "graduate"
    # TODO: 由开发者决定 year 如何影响选课推荐逻辑

    completed_courses: list[str] = field(default_factory=list)
    # 用户已修完的课程列表，用于先修课检查
    # TODO: 格式约定？例如 "CS 161" 还是 "COMPSCI 161"？

    # TODO: 是否需要存储用户偏好（时间偏好、教授偏好等）？


@dataclass
class SessionState:
    """
    单轮会话的完整上下文状态。
    Router 通过读取此对象来做 skill 调用决策。
    """

    user_profile: UserProfile = field(default_factory=UserProfile)

    conversation_history: list[dict] = field(default_factory=list)
    # 格式建议: [{"role": "user"/"assistant", "content": "..."}]
    # TODO: 由开发者决定 history 保留多少轮用于 routing 判断

    current_intent: Optional[str] = None
    # 当前轮次分类出的 intent，由 intent_classifier 写入
    # TODO: 是否需要保留上一轮 intent 用于多轮对话追踪？

    is_course_related: Optional[bool] = None
    # 当前问题是否被判断为选课相关

    skill_invoked: bool = False
    # 本轮是否已调用过 course skill

    # TODO: 由开发者决定是否需要追踪工具调用历史（tool_call_log 等）
    # TODO: 由开发者决定是否需要存储上一步的 handler 结果用于多轮追踪
