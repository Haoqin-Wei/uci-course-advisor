# main.py
#
# 入口文件：接收用户输入，驱动整个 Router → Skill → Handler pipeline。
# 当前阶段用于手动测试和验证 routing 通路是否符合预期。
# ─────────────────────────────────────────────────────────────────────────────

from state.session_state import SessionState, UserProfile
from router.intent_classifier import is_course_related, classify_course_intent
from router.skill_gate import should_invoke_course_skill
from skills.course_skill import invoke_course_skill


def process_user_input(question: str, state: SessionState) -> dict:
    """
    完整的 routing pipeline：从用户输入到 handler 结果。

    Pipeline 流程：
        1. is_course_related()         → 判断是否选课相关
        2. classify_course_intent()    → 分类子意图
        3. should_invoke_course_skill() → 判断是否调用 skill
        4. invoke_course_skill()        → 调用 skill（若需要）

    Args:
        question (str): 用户当前输入。
        state (SessionState): 当前会话状态。

    Returns:
        dict: 包含路由决策过程和最终结果的响应对象。
    """

    response = {
        "question": question,
        "is_course_related": None,
        "intent": None,
        "skill_invoked": False,
        "result": None,
        "reply": None,  # 最终给用户的文本回复
    }

    # ── Step 1: 判断是否选课相关 ──────────────────────────────────────────────
    related = is_course_related(question, state)
    response["is_course_related"] = related
    state.is_course_related = related

    if not related:
        # TODO: 由开发者决定非选课相关问题的处理方式
        # 例如: 交给通用对话模块、返回引导语等
        response["reply"] = "[TODO] 非选课相关问题的处理逻辑尚未实现。"
        return response

    # ── Step 2: 分类子意图 ────────────────────────────────────────────────────
    intent = classify_course_intent(question, state)
    response["intent"] = intent
    state.current_intent = intent

    # ── Step 3: 判断是否需要调用 skill ────────────────────────────────────────
    invoke = should_invoke_course_skill(question, state)

    if not invoke:
        # TODO: 由开发者决定：不调用 skill 时如何回复？
        # 例如: 用静态 FAQ 回答、提示用户补充信息等
        response["reply"] = "[TODO] 选课相关但不需要调用 skill 时的处理逻辑尚未实现。"
        return response

    # ── Step 4: 调用 Course Skill ─────────────────────────────────────────────
    result = invoke_course_skill(intent=intent, question=question, state=state)
    response["skill_invoked"] = True
    response["result"] = result
    response["reply"] = result.get("response", "[TODO] handler 未返回 response 字段。")

    # 更新会话历史
    state.conversation_history.append({"role": "user", "content": question})
    state.conversation_history.append({"role": "assistant", "content": response["reply"]})

    return response


# ─────────────────────────────────────────────────────────────────────────────
# 手动测试入口（临时用，后续替换为真实对话循环或 API 接口）
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # TODO: 由开发者替换为真实的用户 profile（或通过对话收集）
    profile = UserProfile(
        major=None,         # TODO: 填写测试用专业
        school=None,        # TODO: 填写测试用学院
        year=None,          # TODO: 填写测试用年级
        completed_courses=[], # TODO: 填写已修课程
    )
    state = SessionState(user_profile=profile)

    # TODO: 替换为真实测试问题，或接入对话循环
    test_questions = [
        "What are the prerequisites for CS 161?",
        "Can you recommend some CS electives for a junior?",
        "What's the weather like today?",  # 预期：非选课相关
    ]

    for q in test_questions:
        print(f"\n{'='*60}")
        print(f"User: {q}")
        result = process_user_input(q, state)
        print(f"is_course_related : {result['is_course_related']}")
        print(f"intent            : {result['intent']}")
        print(f"skill_invoked     : {result['skill_invoked']}")
        print(f"reply             : {result['reply']}")
