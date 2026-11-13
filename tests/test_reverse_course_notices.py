from copy import deepcopy
from collections import OrderedDict
import pytest
from app.data import course_changes as changes
from app.agent import tools, loop
from tests.test_anteater_payload_preservation import _swe43_response


@pytest.fixture(autouse=True)
def cache(monkeypatch):
    monkeypatch.setattr(changes, "_cache", OrderedDict())


def test_old_code_alone_discovers_new_code_comment_in_same_term(monkeypatch):
    raw = _swe43_response()
    original = deepcopy(raw)
    calls = []
    def fetch(*args, **kwargs):
        calls.append(kwargs['params'])
        return deepcopy(raw)
    monkeypatch.setattr(changes.anteater, '_get_json', fetch)
    monkeypatch.setattr(tools.db, 'get_sections', lambda *a: {
        'offering_status':'not_offered', 'authoritative':True, 'sections':[], 'found':False})
    result = tools._tool_get_sections('IN4MATX43', term='2026 Fall', context={})
    related = result['related_course_notices']
    assert result['offering_status'] == 'not_offered'  # original code still absent
    assert related['candidates'][0]['course_id'] == 'SWE43'
    cid = related['candidates'][0]['comment_context_id']
    assert related['comment_contexts'][cid]['course']['comment'] == raw['data']['schools'][0]['departments'][0]['courses'][0]['courseComment']
    assert any(n['normalized_course_id']=='IN4MATX 43' for n in result['course_code_normalizations'])
    assert calls == [{'year':'2026','quarter':'Fall'}]
    changes.find_related_notices('INF43', '2026 Fall')
    assert len(calls)==1
    assert raw == original


def test_number_changes_also_discovered_and_other_references_not_declared_equivalent(monkeypatch):
    raw = _swe43_response()
    course = raw['data']['schools'][0]['departments'][0]['courses'][0]
    course['courseId']='SWE143'
    course['courseComment']='Prerequisite: INF 43. This is not a replacement.'
    monkeypatch.setattr(changes.anteater,'_get_json', lambda *a,**k: raw)
    found = changes.find_related_notices('IN4MATX43','2026 Fall')
    assert found['status']=='candidate_notices'
    assert found['candidates'][0]['course_id']=='SWE143'
    assert not any(k in found for k in ('equivalent_to','may_take','replacement'))


@pytest.mark.parametrize('body', [None, {'ok':True,'data':{}}, {'ok':False}])
def test_failed_discovery_keeps_known_absence_and_marks_identity_unknown(monkeypatch,body):
    monkeypatch.setattr(changes.anteater,'_get_json',lambda *a,**k: body)
    result=changes.attach_related_notices({'offering_status':'not_offered'},'IN4MATX43','2026 Fall')
    assert result['offering_status']=='not_offered'
    assert result['related_course_notices']['status']=='unavailable'


def test_empty_comments_do_not_invent_successor(monkeypatch):
    raw=_swe43_response()
    raw['data']['schools'][0]['departments'][0]['courses'][0]['courseComment']=''
    monkeypatch.setattr(changes.anteater,'_get_json',lambda *a,**k: raw)
    assert changes.find_related_notices('INF43','2026 Fall')['status']=='no_matching_notice'


def test_positive_code_allows_original_comments_but_absent_code_not_retried():
    context={'_definitive_course_offerings':[{'course_id':'SWE43','term':'2026 Fall','offering_status':'offered'}]}
    args={'course_id':'SWE43','term':'2026 Fall'}
    assert loop._definitive_offering_stop_reason('get_live_sections',args,context=context) is None
    context['_definitive_course_offerings'][0]['offering_status']='not_offered'
    assert loop._definitive_offering_stop_reason('get_live_sections',args,context=context)


def test_api_fallback_keeps_or_prerequisites_instead_of_requiring_all(monkeypatch):
    from app.data import db
    from app.data.prerequisites import evaluate_prerequisite_tree
    tree={'AND':[{'OR':[{'prereqType':'course','courseId':cid,'minGrade':'D-'}
                       for cid in ('I&C SCI 32','I&C SCI H32','I&C SCI 32A')]}]}
    monkeypatch.setattr(db,'_course_info_cache',{})
    monkeypatch.setattr(db,'_find_local_course_record',lambda *_:None)
    monkeypatch.setattr(db.anteater,'fetch_course',lambda *_:{'prerequisiteTree':tree,
        'prerequisiteText':'I&C SCI 32 or I&C SCI H32 or I&C SCI 32A',
        'prerequisites':[{'id':cid} for cid in ('ICS32','ICSH32','ICS32A')]})
    course=db.get_course_info('SWE43')['course']
    result=evaluate_prerequisite_tree(course['prerequisite_tree'],
        completed_courses=[{'course_id':'ICS32','grade':'B+'}],flat_prerequisites=course['prerequisites'])
    assert result.status=='met'


def test_profile_course_alias_keeps_imported_grade(monkeypatch):
    from types import SimpleNamespace
    from app.data import db
    monkeypatch.setattr(db,'get_memory_manager',lambda:SimpleNamespace(get_profile=lambda _: {'completed_courses':['ICS32']}))
    monkeypatch.setattr(db,'get_ai_academic_context',lambda _: {'courses':[{'course_id':'I&C SCI 32','effective_grade':'B+'}]})
    assert db.get_student_profile('synthetic')['profile']['completed_course_attempts']==[
        {'course_id':'I&C SCI 32','grade':'B+'}]
