# Student Profile

Student Profile is a standalone workspace between Ask and Schedule. It keeps the
current chat, unsent input, and schedule state when navigating away. The user-menu
Profile entry opens the same page. Requirements and Courses remain out of scope.

## Data and interactions

- `GET /api/memory/me` supplies major, class standing, saved graduation information,
  manual completed courses, and saved enrolled/waitlisted courses.
- `GET /api/academic/profile` supplies transcript-enriched completed course titles,
  imported units, import timestamp, and GPA availability. An empty completed list
  is authoritative: do not replace it with manual courses excluded by transcript
  pass/fail records. Manual courses are only the fallback when academic data fails.
- GPA is hidden initially and on leaving or refreshing the page. Only clicking
  Show GPA requests `?include_gpa=true`. The endpoint authenticates the current
  account and returns `Cache-Control: no-store`; late reveal responses cannot
  redisplay GPA after navigation.
- Units come from the transcript's `units_completed`. Missing units or graduation
  fields show an unset state. Do not infer graduation from class standing or
  hardcode the reference image's 180-unit degree requirement.
- This quarter shows the existing profile's enrolled/waitlisted lists. These are
  user-saved records, not live registrar enrollment; Schedule drafts are separate.
- Edit opens the prefilled existing onboarding editor. Closing it refreshes the
  profile, including partial saves. Its final coursework step scrolls as one page;
  the save footer stays visible and the A–Z catalog index remains sticky. Do not
  constrain its catalog to the height left below the transcript/selection controls:
  large imported course lists otherwise push the catalog out of view.
  Transcript import uses the existing local PDF
  parser and allow-listed server payload, then refreshes this page and the sidebar.
- A successful new PDF import replaces the current account's entire academic
  snapshot: prior imported and manually selected completed courses, GPA, units,
  exam/transfer credits, and university requirement status. Fields missing from
  the new PDF clear old values, and the latest successful upload wins regardless
  of its printed date. For example, importing B's 19 courses and 3.3 GPA after A's
  10 courses and 4.0 GPA displays only B's academic records. File parsing, request
  validation, and academic database write failures leave the previous academic
  snapshot intact; its replacement is one database transaction. Historical
  import metadata is retained only for request idempotency.
  Account identity/login and manually supplied fields outside the parser's schema,
  including major and expected graduation, keep their existing values.
- The planning link returns to Ask and fills an editable prompt using the existing
  default term only if the composer is empty. It never overwrites an unsent question,
  submits automatically, or changes the default term. See [term rules](term-rules.md).
- Delete account opens the existing password/confirmation flow.

The two-column course list supports subject grouping, course-code A–Z order, and
search across codes, titles, and subject names. On phones it becomes one column.
Department names are optional enrichment and must not block the profile itself.

## Verification

Run `pytest tests/test_academic_transcript_import.py tests/test_frontend_static_contract.py
tests/test_profile_context_characterization.py` for authenticated academic reads
and existing integration contracts. Serve the repository locally, then run
`node scripts/verify_student_profile_ui.cjs` (Playwright and Chrome required) for
isolated browser checks. API responses and imported PDF extraction are fixtures;
the test performs no real account changes. Run `scripts/verify_solon_ui.cjs` for
the Ask/Schedule regression flow.
