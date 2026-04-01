# tests/test_router.py
#
# 职责：验证 Router 层的核心函数行为是否符合预期。
# 当前阶段：测试桩，等业务规则填写完成后补充具体断言。
#
# 运行方式: pytest tests/test_router.py -v
# ─────────────────────────────────────────────────────────────────────────────

import pytest
from state.session_state import SessionState, UserProfile


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def empty_state():
    """无任何 profile 信息的空白会话状态"""
    return SessionState()


@pytest.fixture
def cs_junior_state():
    """
    模拟一个 CS 专业大三学生的会话状态。
    TODO: 填写实际测试用的 profile 数据。
    """
    profile = UserProfile(
        major="Computer Science",
        school="Donald Bren School of ICS",
        year="junior",
        completed_courses=["ICS 31", "ICS 32", "ICS 33", "CS 161"],
        # TODO: 补充更多测试用数据
    )
    return SessionState(user_profile=profile)


# ─────────────────────────────────────────────────────────────────────────────
# is_course_related() 测试
# ─────────────────────────────────────────────────────────────────────────────

class TestIsCourseRelated:

    def test_course_related_question_returns_true(self, empty_state):
        """
        TODO: 填写应该返回 True 的测试用例（选课相关问题）。
        在 config/routing_rules.py 填写规则后完善此测试。
        """
        from router.intent_classifier import is_course_related
        # TODO: 替换为实际触发 True 的问题
        question = "[TODO: 填写一个应被判断为选课相关的问题]"
        # assert is_course_related(question, empty_state) == True
        pytest.skip("TODO: 业务规则尚未填写，跳过此测试")

    def test_non_course_question_returns_false(self, empty_state):
        """
        TODO: 填写应该返回 False 的测试用例（非选课问题）。
        """
        from router.intent_classifier import is_course_related
        question = "[TODO: 填写一个应被判断为非选课相关的问题]"
        # assert is_course_related(question, empty_state) == False
        pytest.skip("TODO: 业务规则尚未填写，跳过此测试")

    def test_ambiguous_question_with_context(self, cs_junior_state):
        """
        TODO: 测试模糊问题在有上下文时的判断结果。
        例如："这门课好不好学？" 在有上下文时是否应被视为选课相关？
        """
        pytest.skip("TODO: 多轮上下文规则尚未定义，跳过此测试")


# ─────────────────────────────────────────────────────────────────────────────
# classify_course_intent() 测试
# ─────────────────────────────────────────────────────────────────────────────

class TestClassifyCourseIntent:

    # TODO: 为每个 COURSE_INTENT_TYPES 中的子意图各写一个测试用例
    # 例如:
    #
    # def test_prerequisite_check_intent(self, empty_state):
    #     from router.intent_classifier import classify_course_intent
    #     question = "[TODO: 填写一个先修课相关问题]"
    #     assert classify_course_intent(question, empty_state) == "prerequisite_check"
    #
    # def test_unknown_intent_fallback(self, empty_state):
    #     from router.intent_classifier import classify_course_intent
    #     question = "[TODO: 填写一个无法分类的选课问题]"
    #     assert classify_course_intent(question, empty_state) == "unknown"

    def test_placeholder(self):
        pytest.skip("TODO: 在 config/routing_rules.py 中填写 INTENT_CLASSIFICATION_RULES 后实现此测试")


# ─────────────────────────────────────────────────────────────────────────────
# should_invoke_course_skill() 测试
# ─────────────────────────────────────────────────────────────────────────────

class TestShouldInvokeSkill:

    def test_skill_not_invoked_twice(self, empty_state):
        """
        验证：如果 state.skill_invoked == True，不应重复调用 skill。
        这条规则已在 skill_gate.py 中有基础实现，填写规则后可直接启用。
        """
        from router.skill_gate import should_invoke_course_skill
        empty_state.skill_invoked = True
        question = "[TODO: 任意问题]"
        # assert should_invoke_course_skill(question, empty_state) == False
        pytest.skip("TODO: 实现 should_invoke_course_skill() 后启用此测试")

    def test_skill_invoked_for_complex_request(self, cs_junior_state):
        """
        TODO: 验证复杂的个性化推荐请求应触发 skill 调用。
        """
        pytest.skip("TODO: 业务规则尚未填写，跳过此测试")

    def test_skill_skipped_for_incomplete_profile(self, empty_state):
        """
        TODO: 验证 profile 信息不完整时是否跳过 skill 调用。
        """
        pytest.skip("TODO: 业务规则尚未填写，跳过此测试")
