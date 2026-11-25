# Live vehicle tracking

RapidGrid takes a vehicle's position from one of two places on the crew's phone.

| | Crew screen ("Share my location") | Tracker app (Traccar Client) |
|---|---|---|
| Set-up | None: switch it on in the crew screen | Install a free app, enter two settings |
| Screen locked or app in background | **Stops sending** | Keeps sending |
| Best for | A short demo with the screen kept on | Real vehicles, whole shifts |

Use the tracker app for anything real. A browser cannot send GPS reliably in
the background, and the phone stops it to save battery.

## What the server does with a fix

- **Checks:** a fix outside the service area is refused (the tracker app
  still gets a 200). A fix delivered out of order, or taken more than
  2 minutes ago, is accepted but not used.
- **Real vehicles stay real:** once a vehicle's phone has sent a fix on a
  call, the simulator never moves that vehicle for the rest of the call. If
  the phone goes quiet (a tunnel, a flat battery), the dispatch screen shows
  "GPS signal lost: last fix N s ago". It never invents movement.
- **Route matching:** each fix is matched only to the stretch of route the
  vehicle can have reached since its last fix. A noisy fix cannot jump it to
  a later part of a route that passes close to itself. Fixes worse than ±50 m
  do not move its progress, and a fix with no accuracy is treated as ±50 m.
- **After the call:** the vehicle stays where it last was. It is not sent
  back to its station.

## Setting up a vehicle with Traccar Client

1. **Make a key.** Give the vehicle a secret tracker key in the contacts
   directory (`RAPIDGRID_CONTACTS`), on its unit entry:
   `"tracker_key": "trk_<32 random hex characters>"`.
   Make the random part with `openssl rand -hex 16`.
   The key is the vehicle's only credential, so treat it like a password.
   Wrong keys are throttled, like wrong PINs.
2. **Install the app.** Install **Traccar Client** on the crew phone. It is
   free on the Google Play Store and the App Store.
3. **Enter the settings** in the app:
   - **Device identifier:** the vehicle's tracker key.
   - **Server URL:** `https://<your RapidGrid host>/api/track`
   - **Location accuracy:** High.
   - **Frequency / interval:** 5 seconds. **Distance:** 0.
4. **Allow the app to run in the background.**
   - **Android:** set Location permission to "Allow all the time", and set
     Battery to "Unrestricted" for Traccar Client.
   - **iOS:** set Location to "Always", with Precise Location on.
5. **Start it** by turning on the service switch in the app. Within a few
   seconds the vehicle shows "live GPS" on the dispatch map.

The endpoint also accepts OsmAnd's live-tracking format:
`/api/track?id=<key>&lat=..&lon=..&timestamp=..&accuracy=..&speed=<knots>&bearing=..`

**Privacy:** the server URL contains no secret. Traccar Client 9 and later
send the key in the request body. OsmAnd and older Traccar Client versions
put it in the query string, where it can appear in access logs; with those,
rotate keys more often. Rotate a key by changing it in the contacts
directory; the old key stops working when the service restarts.

**A fix that can't be used** (outside the area, too soon after the last one,
or too old) is still answered with 200, so the app's queue never stalls on
it. **Set the phone's date and time to automatic:** a clock more than
2 minutes behind makes every fix look old, and none will be used.

## Before relying on it

- Drive the route once with the app running and check the dispatch screen.
  The vehicle should follow the road, with no "off route" alerts.
- The road model covers Bengaluru (about 12.8–13.1 N, 77.45–77.78 E).
  Fixes outside it are refused.
- Keep the crew's usual radio or phone procedure as the fallback.
