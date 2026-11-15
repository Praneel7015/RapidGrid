# Independent review and repaired failures

The changes were checked through three initial review lenses: an elephant pass
with the live-test history, a fresh-context goldfish pass over the source, and a
separate reliability/security pass. The goldfish reviewer then challenged the
backend changes independently. After an interrupted work session, a further
fresh security review and backend completion pass checked the saved changes.

The initial live review used two labeled synthetic incidents and completed both
after testing. Subsequent regression tests run only against isolated local data.
Application changes have not been deployed by opening this PR.

## Repaired behavior

| Area | Failure found | Change |
| --- | --- | --- |
| Citizen | Refresh returned to SOS while an incident remained active | Recover the actual active record and its processing/tracking state |
| Driver | Pickup removed the case from the active list | Keep medical transport active and derive the stage from server state |
| Multiple units | Primary and backup shared one claim/status; one arrival could finish both | Track claims and progress independently; finish only after required units complete |
| State transitions | Driver claim could bypass approval; actions could rewind terminal state | Validate transitions and reject conflicting changes |
| Hospital | Details returned 500; capacity controls called a missing endpoint | Separate GET details and PATCH capacity, validate and persist overrides |
| Diversion | An in-flight route could select a facility newly on divert | Revalidate the destination before committing the transition |
| Hospital scope | Loose name-token matching showed unrelated cases | Match resolved facility IDs exactly |
| Chat | First socket connection reset history; desktop panels covered Send | Persist history without reset; keep a bounded accessible chat layout |
| UI errors | Failed HTTP actions could appear successful | Show errors, preserve the phase and confirm server changes before committing UI state |
| Storage | Process memory/JSON on ephemeral container storage | Atomic SQLite snapshots with an explicit persistent-volume configuration |
| Restart | Interrupted processing could remain stuck | Mark it failed with a retry explanation |
| Access | Operational APIs/sockets exposed without server protection | Shared operator showcase session across HTTP and sockets; protect bulk clears even in demo mode |
| Health | A nonempty key was reported as live routing | Track actual provider results, freshness, active source and fallback |
| Responsiveness | Synchronous routing blocked asynchronous request handling | Run blocking routing/path calculations away from the event loop |
| Homepage | Runner-up was described as closer regardless of travel time | Compare actual ETAs and distinguish sample/estimated values |
| Notifications | A forced mock SMS timeout looked like an outage; mocks claimed delivery | Explicit simulated receipts and an optional failure scenario |

## Validation scope and limits

Backend tests cover the state machine, storage reload, independent unit
assignments, diverted destinations, first-connect history, session boundaries,
provider errors and malformed responses, missing-graph startup and non-blocking
routing. Browser tests cover the complete medical workflow and the concrete
refresh, chat, capacity and responsive-layout regressions.

This remains a showcase. Shared operator access does not implement real
per-user/per-facility authorization. Data and coordination require a single
process; interrupted orchestration fails visibly rather than resuming. Bed
counts, fleet positions, notification channels and clinical outcome estimates
remain modeled or simulated. Triage is keyword scoring, not a trained model.
Microphone/browser speech behavior and real provider quota policies require
separate device/account checks; the regression suite does not claim to validate
those external systems.

Deployment setup and its paid disk/instance requirements are described in
[SHOWCASE.md](SHOWCASE.md). Existing working provider credentials need not be
replaced to use these fixes; Google billing ownership remains a separate account
verification task.
