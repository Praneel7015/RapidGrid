# Team demo: five people, five screens

One live system, five points of view. Everything one person does appears on
the other screens within a few seconds.

## Before you go on stage (15 minutes before)

1. **Unlock** every laptop on the hosted site with the operator password.
2. **Take seats.** On each laptop open `/seats` and press that person's
   seat. The laptop remembers it, and a coloured strip at the top of every
   screen says whose screen it is.

   | Seat | Screen | Who |
   |---|---|---|
   | 1 | Caller (`/citizen`) | Person 1 (a phone works too) |
   | 2 | Dispatcher (`/dispatcher`) | Person 2 |
   | 3 | Ambulance crew (`/driver`) | Person 3 |
   | 4 | ER desk (`/hospital`), Aster CMI Hospital (Hebbal) | Person 4 |
   | 5 | City control room (`/control`), on the projector | Person 5 |

3. **Reset the demo** from the dispatcher's control-room log.
4. **Start a busy city** from the control room, about 2 minutes before you
   start. Four other emergencies begin across Bengaluru, already under way.
   Also turn on "New call every 2 min".

   These calls are labelled "simulated" everywhere, and they never use
   AMB-01, AMB-02 or AMB-03, so your scripted call always gets its ambulance.
5. **Presenter phones:** keep "Share my location" off, unless the crew is
   really driving with GPS (see `docs/LIVE-TRACKING.md`).

## The story (about 5 minutes)

| Time | Who | Does | Everyone sees |
|---|---|---|---|
| 0:00 | Projector (5) | "This is Bengaluru right now: 30 hospitals, 11 stations, 17 vehicles, emergencies already under way." | A city in motion |
| 0:30 | Caller (1) | Reports: "My father collapsed with severe chest pain near Hebbal." Voice or text | A new red **!** on the projector |
| 0:45 | Dispatcher (2) | Opens the call, reads the triage and hospital ranking, presses **Approve** | The caller sees "help is on the way"; an ambulance turns orange |
| 1:15 | Crew (3) | Opens the call and confirms it. Route and live time to destination | The vehicle moves on every map |
| 1:45 | Dispatcher (2) | Presses **Close road ahead**, the disruption ahead of the unit | |
| 2:00 | Dispatcher (2) | The agent's red alert: cause, delay, new arrival time, routes around it, keep or send a backup. Reads "How the agent decided", then **Accepts** the reroute | The crew's route changes; the caller sees the new time; the decision is posted in the call's chat |
| 3:00 | Crew (3) | **Patient picked up** | The ER desk sees the patient coming |
| 3:30 | ER desk (4) | Optional: goes **on divert**. The agent proposes the next hospital, and dispatch accepts | The hospital turns grey on the map |
| 4:30 | Crew (3) | **Handover** at the ER | The caller gets the closing summary |
| 5:00 | Projector (5) | The control-room log: every decision the agent made, with its reasons | |

## If something goes wrong

- **"No ambulance is free":** reset the demo and start the busy city again.
- **A screen looks stale:** reload it. The seat and the sign-in are remembered.
- **Hosted site asleep or down:** the guided demo (`/demo`) needs the backend. Either
  run the app on the presenter's laptop (backend and frontend, see
  [DEVELOPMENT.md](../DEVELOPMENT.md)), or fall back to the screens-only recovery steps
  in this document: the printed route map and the recorded video.
