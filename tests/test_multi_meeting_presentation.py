"""Full meeting evidence survives recommendation and calendar presentation."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from app.recommendations.presentation import normalize_recommendation_card, render_recommendation_answer


ROOT = Path(__file__).resolve().parents[1]


def _meetings():
    return [
        {"days": "M", "startTime": {"hour": 9, "minute": 0}, "endTime": {"hour": 9, "minute": 50},
         "bldg": ["DBH 1100"], "timeIsTBA": False, "source_detail": "first arrangement"},
        {"days": "F", "startTime": {"hour": 14, "minute": 0}, "endTime": {"hour": 14, "minute": 50},
         "bldg": ["ICS 174"], "timeIsTBA": False, "source_detail": "second arrangement"},
    ]


def _card(meetings):
    primary = {"code": "36120", "num": "A", "type": "Lec", "instructors": ["EXAMPLE, A."],
               "days": "M", "start_time": "09:00", "end_time": "09:50", "time": "M 09:00-09:50",
               "meetings": meetings}
    alternative = {**deepcopy(primary), "code": "36121", "num": "B", "meetings": list(reversed(_meetings()))}
    secondary = {**deepcopy(primary), "code": "36122", "num": "A1", "type": "Dis"}
    return {"course_id": "COMPSCI 161", "term": "2026 Fall", "primary_code": "36120",
            "sections": [deepcopy(primary), deepcopy(alternative), deepcopy(secondary)],
            "section_groups": [{"primary": primary, "secondaries": [secondary]},
                               {"primary": alternative, "secondaries": []}]}


def test_all_raw_meetings_survive_selected_section_and_alternatives_without_input_mutation():
    card = _card(_meetings())
    before = deepcopy(card)
    normalized = normalize_recommendation_card(card)
    assert card == before
    selected = normalized["presentation"]["selected_lecture"]
    assert selected["meetings"] == before["section_groups"][0]["primary"]["meetings"]
    assert selected["meeting_schedule"][1]["start_time"] == "14:00"
    for field in ("sections", "section_groups"):
        assert normalized[field] == before[field]
    answer = render_recommendation_answer([normalized], "en")
    for arrangement in ("M 09:00–09:50 (DBH 1100)", "F 14:00–14:50 (ICS 174)"):
        assert arrangement in answer
    assert normalize_recommendation_card(normalized) == normalized


@pytest.mark.parametrize("meetings", [[], {}, "bad source array", [None]])
def test_explicit_empty_or_malformed_meetings_do_not_display_stale_flat_time(meetings):
    card = _card(meetings)
    normalized = normalize_recommendation_card(card)
    selected = normalized["presentation"]["selected_lecture"]
    assert selected["meetings"] == meetings
    assert selected["time"] == "TBA"
    assert "09:00" not in render_recommendation_answer([normalized])


def test_legacy_none_meetings_preserve_existing_time_without_repeated_days():
    normalized = normalize_recommendation_card(_card(None))
    assert normalized["presentation"]["selected_lecture"]["meetings"] is None
    answer = render_recommendation_answer([normalized])
    assert "M 09:00-09:50" in answer
    assert "M M 09:00" not in answer


def test_mixed_known_and_tba_meetings_both_appear_in_controlled_text():
    meetings = [*_meetings(), {"days": "W", "timeIsTBA": True, "bldg": ["Online"]}]
    answer = render_recommendation_answer([_card(meetings)], "en")
    assert "M 09:00–09:50" in answer and "F 14:00–14:50" in answer
    assert "W TBA (Online)" in answer


def test_js_cards_and_calendar_keep_second_meetings_and_partial_unknowns_visible():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for frontend meeting behavior checks")
    script = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const root = process.argv[1];
const text = value => String(value ?? '');
const escHTML = value => text(value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
const escAttr = value => escHTML(value).replace(/"/g, '&quot;').replace(/'/g, '&#39;');
const context = vm.createContext({console, escHTML, escAttr, planObjects: new Map(), scheduleEvents: [],
  planTerm: card => card.term, scheduleCourseKey: (term, id) => `${term}|${id}`,
  canonicalTerm: value => value, addedCourses: new Set(), pendingScheduleEntries: [],
  exactUnits: value => Number.isFinite(value) ? value : null,
  displayTime: value => value, _prettyCourseId: value => value});
vm.runInContext(fs.readFileSync(`${root}/static/js/cards.js`, 'utf8'), context);
vm.runInContext(fs.readFileSync(`${root}/static/js/schedule.js`, 'utf8'), context);
vm.runInContext(fs.readFileSync(`${root}/static/js/plan.js`, 'utf8'), context);
const meeting = (days, hour, room) => ({days, startTime: {hour, minute: 0}, endTime: {hour, minute: 50}, bldg: [room]});
const section = {section_num: 'A', section_code: '36120', days: 'M', start_time: '09:00', end_time: '09:50',
  time: 'M 09:00-09:50', meetings: [meeting('M', 9, 'DBH 1100'), meeting('F', 8, '<img src=x onerror=evil()>')]};
context.section = section;
const times = vm.runInContext('_fmtSectionDaysTime(section)', context);
assert.equal(times, 'M 09:00-09:50<br>F 08:00-08:50');
const rooms = vm.runInContext('_fmtSectionLocations(section)', context);
assert.ok(rooms.includes('DBH 1100<br>&lt;img'));
assert.ok(!rooms.includes('<img'));
const entry = {course_id: 'COMPSCI161', section: 'A', term: '2026 Fall', units: 4, materialized_section: section};
const events = [{course_id: 'COMPSCI161', section_num: 'A', section_code: '36120', term: '2026 Fall',
  day: 'Mon', start: '09:00', end: '09:50', meeting_index: 0},
  {course_id: 'COMPSCI161', section_num: 'A', section_code: '36120', term: '2026 Fall',
  day: 'Fri', start: '08:00', end: '08:50', meeting_index: 1}];
context.entry = entry; context.events = events;
let metrics = vm.runInContext('scheduleMetrics([entry], events)', context);
assert.equal(metrics.earliest, '8:00');
assert.equal(metrics.friday, 'Free');
assert.equal(metrics.incomplete, false);
section.meetings.push({days: 'W', timeIsTBA: true, bldg: ['Online']});
metrics = vm.runInContext('scheduleMetrics([entry], events)', context);
assert.equal(metrics.incomplete, true);
assert.equal(metrics.friday, 'Unknown');
assert.equal(vm.runInContext('scheduleEntryStatus(entry, events)', context), 'tba');
assert.equal(vm.runInContext('scheduleEntryHasEvent(entry, events)', context), true);
const untimed = vm.runInContext('renderUntimedSchedule([entry], events)', context);
assert.ok(untimed.includes('Time TBA') && untimed.includes('Confirmed meetings still appear'));
events[1].end = '14:50';
assert.equal(vm.runInContext('scheduleMetrics([entry], events).friday', context), 'Busy');
for (const invalid of [[], {}, [null]]) {
  section.meetings = invalid;
  assert.equal(vm.runInContext('_fmtSectionDaysTime(section)', context), 'TBA');
  assert.equal(vm.runInContext('scheduleMetrics([entry], events).incomplete', context), true);
}
section.meetings = null;
assert.equal(vm.runInContext('_fmtSectionDaysTime(section)', context), 'M 09:00-09:50');
assert.equal(vm.runInContext('scheduleEntryStatus(entry, events)', context), 'resolved');
section.meetings = [{days: 'TuTh', start_time: '3:30', end_time: '4:50p', location: 'ICS 174'}];
assert.equal(vm.runInContext('_fmtSectionDaysTime(section)', context), 'TuTh 15:30-16:50');
context.sameDay = {...events[0], meeting_index: 1, start: '11:00', end: '11:50'};
assert.notEqual(vm.runInContext('scheduleEventKey(events[0])', context), vm.runInContext('scheduleEventKey(sameDay)', context));
assert.equal(vm.runInContext('_computeOverlapLayout([events[0], sameDay]).size', context), 2);
// Compact plan rows use the same full evidence, including unknown meetings.
context.card = {course_id: 'COMPSCI161', term: '2026 Fall', primary_code: '36120', sections: [section]};
section.meetings = [meeting('M', 9, 'DBH'), {days: 'W', timeIsTBA: true}];
assert.equal(vm.runInContext('planMeetingLabel(card)', context), 'M 09:00-09:50; W TBA');
context.addedCourses.add('2026 Fall|COMPSCI161');
context.scheduleEvents = [events[0], context.sameDay];
context.pendingScheduleEntries = [{...entry, materialized_section: {...section,
  meetings: [meeting('F', 8, 'ICS'), {days: 'Tu', timeIsTBA: true}]}}];
assert.equal(vm.runInContext('planMeetingLabel(card)', context), 'F 08:00-08:50; Tu TBA');
context.pendingScheduleEntries[0].materialized_section.meetings = [];
assert.equal(vm.runInContext('planMeetingLabel(card)', context), 'TBA');
context.pendingScheduleEntries[0].materialized_section.meetings = null;
assert.equal(vm.runInContext('planMeetingLabel(card)', context), 'M 09:00–09:50; M 11:00–11:50');
context.pendingScheduleEntries = [];
section.meetings = null;
assert.equal(vm.runInContext('planMeetingLabel(card)', context), 'M 09:00–09:50; M 11:00–11:50');
console.log(JSON.stringify({times, partialUnknown: true, eventKeysDistinct: true}));
"""
    result = subprocess.run([node, "-e", script, str(ROOT)], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout)["eventKeysDistinct"] is True
