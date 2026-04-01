# router/dispatcher.py
#
# 职责：根据分类出的 intent，将请求路由到对应的 handler 模块。
# 这是 Router 层的最后一步，决定"谁来处理这个问题"。
#
# 设计原则：
#   - dispatcher 只负责路由，不处理业务逻辑
#   - 路由规则来自 config/routing_rules.py 中的 INTENT_TO_HANDLER_MAP
#   - handler 的具体实现在 handlers/ 目录下
#
# 依赖：
#   - config/routing_rules.py
#   - handlers/*.py（本阶段均为 stub）
# ─────────────────────────────────────────────────────────────────────────────

from typing import Any, Optional
from config.routing_rules import INTENT_TO_HANDLER_MAP
from state.session_state import SessionState


# ─────────────────────────────────────────────────────────────────────────────
# 类型别名（便于后续扩展）
# ─────────────────────────────────────────────────────────────────────────────

# HandlerResult 暂定为 dict，后续可以替换为具体的 Response 数据类
HandlerResult = dict[str, Any]


# ─────────────────────────────────────────────────────────────────────────────
# 函数 4：route_course_request
# ─────────────────────────────────────────────────────────────────────────────

def route_course_request(
    intent: str,
    question: str,
    state: SessionState,
) -> HandlerResult:
    """
    根据 intent 将选课请求路由到对应的 handler，并返回处理结果。

    此函数在 should_invoke_course_skill() 返回 True 后调用。

    Args:
        intent (str): 由 classify_course_intent() 分类出的子意图类型。
                      取值应来自 COURSE_INTENT_TYPES。
        question (str): 用户当前输入的原始文本（透传给 handler 使用）。
        state (SessionState): 当前会话状态（透传给 handler 使用）。

    Returns:
        HandlerResult (dict): handler 的处理结果。
            建议包含以下字段（TODO: 由开发者定义具体结构）:
            {
                "success": bool,        # 是否处理成功
                "intent": str,          # 实际路由到的 intent
                "handler": str,         # 实际调用的 handler 名称
                "response": str,        # 给用户的回复文本（stub 阶段可为占位文本）
                "data": dict,           # handler 返回的结构化数据（可选）
                "error": str | None,    # 错误信息（如有）
            }

    路由逻辑（基于 INTENT_TO_HANDLER_MAP）：
        TODO: 由开发者在 config/routing_rules.py 中填写映射关系，
        本函数根据映射动态调用对应 handler。

    异常处理：
        - 如果 intent 不在 INTENT_TO_HANDLER_MAP 中，路由到 fallback handler
        - TODO: 由开发者决定 fallback handler 是什么
    """

    # step 1: 查找对应的 handler
    handler_name = INTENT_TO_HANDLER_MAP.get(intent)

    if handler_name is None:
        # TODO: 由开发者决定 intent 未命中时的处理策略
        # 选项 A: 路由到 general_handler 作为兜底
        # 选项 B: 返回错误，要求用户澄清
        # 选项 C: 记录日志后继续尝试 LLM 直接回复
        handler_name = INTENT_TO_HANDLER_MAP.get("unknown")

    if handler_name is None:
        # 兜底：INTENT_TO_HANDLER_MAP 中连 "unknown" 都没配置
        raise ValueError(
            f"路由失败：intent='{intent}' 未找到对应 handler，"
            "且 INTENT_TO_HANDLER_MAP 中未配置 'unknown' 兜底。"
            "请在 config/routing_rules.py 中补充路由配置。"
        )

    # step 2: 动态加载并调用 handler
    # TODO: 由开发者决定 handler 的调用方式：
    #   方案 A: 动态 import（灵活但较复杂）
    #   方案 B: 显式 if-else 映射到函数（简单直观，适合 demo 阶段）
    #   方案 C: 注册表模式（registry pattern）
    #
    # 本阶段使用方案 B 的占位结构，等 handler 实现后替换：

    # TODO: 由开发者实现具体 handler 调用逻辑
    # 示例结构（不含实际逻辑，handler 均为 stub）:
    #
    #   if handler_name == "handlers.prerequisite_handler":
    #       from handlers.prerequisite_handler import handle
    #       return handle(question, state)
    #   elif handler_name == "handlers.schedule_handler":
    #       from handlers.schedule_handler import handle
    #       return handle(question, state)
    #   ...

    raise NotImplementedError(
        f"route_course_request() 尚未实现 handler 调用逻辑。"
        f"当前 intent='{intent}' 映射到 handler='{handler_name}'。"
        "请实现对应 handler 后，在此处添加调用代码。"
    )
