# GPS map alignment and receiver placement

Select a saved floor in **Map & Setup**, then open the separate **Map alignment** tab.
This optional tool aligns your existing property image with real coordinates.
It uses no map tile service and changes no receiver calibration automatically.

1. Choose a known point on the map with **Pick map point**, or select an existing
   receiver and **Use receiver map point**. Enter its latitude, longitude and
   estimated accuracy in metres. Use zero accuracy only for surveyed coordinates.
   Give the point a label and press **Add anchor**.
2. Add another anchor several metres away, preferably across the property. The
   anchors define translation, rotation and pixels per metre. Two anchors always
   fit exactly, so add a third or more to check whether the points agree. Use
   well-separated points surrounding the receivers you intend to place.
3. Read the anchor accuracy, residual and difference from the existing map scale.
   The GPS-derived scale is informational. Use the existing Set Scale tool with
   a measured distance if the floor's scale needs correction. Review mismatched
   anchors before placing receivers.
4. For GPS captured from your phone, press **Load phone locations** and explicitly
   choose your own Companion App `device_tracker` or `person` entity, if desired.
   Leave **Browser only** to use this device's browser. Press **Capture stationary
   GPS** while standing at the known anchor or receiver. This button and the opt-in automatic survey request browser location permission. Browser location generally
   requires HTTPS and the phone/browser's location permission.
5. Stay still for 30–60 seconds. Capture stops after 45 seconds, or you can press
   **Stop capture** after 30 seconds. A source must provide distinct fresh fixes
   spanning at least 20 seconds. Unchanged HA coordinate/accuracy tuples are counted once, so an entity
   that updates infrequently may not supply enough evidence; use the browser or
   request more frequent Companion App location updates. **Use captured GPS**
   copies the chosen location into the anchor inputs.
6. To move an existing receiver, select it in the GPS receiver menu. For a new
   station, use the existing **Place Receiver** tool, click the map to open its
   picker, select the correct station, then choose **Current Place Receiver
   picker** in the GPS menu. Capture GPS at the physical station, press **Preview
   receiver position**, and inspect the cyan dot and accuracy circle. Press
   **Confirm receiver position** to stage the position. For a new station, press
   the receiver tool's **Save** to complete its normal identity checks.
7. Press **Save alignment** in this tab (or **Save Floor Plan** in Map & Setup) to persist anchors and staged receiver positions.

Automatic source preference compares only the browser and your selected HA
entity. HA freshness uses `last_updated`, which is the state publication time;
raw GPS measurement time is unknown and unrelated attribute updates cannot prove
a new GPS measurement. HA calculates the state age on its own clock; capture
includes request duration and uses that age, so phone and HA clocks can differ.
The capture labels this timestamp provenance. It chooses
the eligible stationary source with the smaller conservative
accuracy, and rejects incompatible locations or observations indicating
movement. It never selects someone else's phone automatically. Missing, denied
or stale browser/HA data can be replaced by the other valid source. Anchors and
GPS placement are limited to local properties within 5 km and latitudes between
−85° and 85°; dateline crossings are supported.

A phone reporting 1 metre accuracy does not guarantee 1 metre map placement.
Repeated GPS samples share errors, so the tool retains the larger of reported
accuracy and measured spread. The preview also includes anchor accuracy, fit
residual and additional uncertainty when extrapolating beyond the anchors.
Indoors or near buildings, correct a coarse GPS placement by dragging the
receiver or placing it manually. GPS does not change radio corrections or use
guessed dog positions for calibration.

Capture watches and timers stop on cancellation, floor/source changes, layout
edits or leaving the page. A changed or deleted receiver cannot accept an old
preview. Anchors persist in private Home Assistant `.storage` only when the
floor plan is saved. Full local diagnostic downloads include saved/staged
anchors and the chosen capture's source, age, sample count, reported accuracy
and spread; inspect these private details before sharing a download.


## Semi-automated receiver survey

After saving at least two alignment anchors, open **Automatic phone receiver survey**.
Load the phones, select your actual Bermuda BLE phone tracker, and optionally
select your Companion App GPS entity. **Start auto survey** watches the browser
GPS and the selected HA location source while this page remains open and visible.
Walk to a receiver and hold the phone still nearby. Fresh Bermuda ranges of at
most 1 metre identify the station; they are independent of GPS accuracy.

BPS collects distinct GPS fixes for at least 30 seconds, averages the eligible
stationary source and records the observed spread. It retains conservative GPS
and map-anchor accuracy rather than claiming repeated samples guarantee 1 metre.
Two simultaneously qualifying receivers are ambiguous in automatic mode. For co-located stations, select the intended placed receiver in the existing Receiver / anchor point menu before starting; only that station’s own fresh range can qualify it. Stale ranges, changed hardware identities, changed alignment or a
capture interrupted by leaving the page do not qualify.

Each candidate shows a proposed map position and accuracy circle. Review it and
press **Verify and save** to update only that receiver's coordinates. Other saved
settings and radio calibration are retained. Pending candidates remain in private
Home Assistant storage so you can verify several stations after a walk.
Use **Remove survey record** to free space occupied by an old verified survey;
its receiver position stays saved. Records from other or removed floors remain
listed for cleanup, while verification requires opening the candidate's floor.

If the verification response is lost, full layout saves pause so they cannot undo
a receiver move that may already be saved. Use **Retry verification** to confirm
the result. If newer receiver or alignment edits prevent reconciliation, reload
the page to read the saved layout; reloading discards unsaved edits.
If the receiver position saves but its survey audit cannot be persisted, BPS
shows a warning and keeps verification available. Retry after storage recovers
or old records are removed; recovery does not move the receiver again.
Full layout saves also reject an older browser tab's data if another client or
calibration updated the saved layout. Retain your unsaved edits separately and
reload the page before saving again. After upgrading, refresh old BPS tabs so
their saves include the current layout revision.

For API clients, obtain `layout_revision` together with `coordinates` from
`GET /api/bps/read_text`, then include it in the multipart full-layout save to
`POST /api/bps/save_text`. A mismatch or missing revision returns HTTP 409 before
layout or map-file changes. Successful saves return the new revision in JSON and
`X-BPS-Layout-Revision`. Survey verification returns both the saved revision and
the revision reconstructed with that receiver's original coordinates, allowing
the frontend to reconcile that coordinate change without assuming other fields
are still current. Shared survey capacity rejects overflow without evicting other
users' records; remove one of your retained records to free space.
Failure to remove an obsolete map file after a successful layout commit returns
the new revision with a cleanup warning, allowing subsequent edits to save.
If storage write verification fails, all layout writers pause until a successful
page refresh reloads the durable layout and calibration geometry. A failed
reload retains the write barrier; resolve the Home Assistant storage problem
and refresh again. Retain unsaved edits separately before refreshing.
After a failed first save, a missing Store can recover to the original empty
layout, allowing a retry without restarting Home Assistant.

The optional **Request fresh phone GPS** toggle uses your explicitly selected
Companion App notification service to send `request_location_update`, at most once
per minute during the survey. Delivery is a request, not confirmation of a GPS
fix. The phone must permit location access; background updates depend on its OS
and Companion App permissions. Stop the survey when finished.

For desktop alignment, **Pick map point** takes you to the map and returns to
alignment after clicking. Enter each point's latitude and longitude separately, or paste a coordinate pair into Latitude;
accuracy can be 0 for surveyed coordinates or an estimate such as 2 metres.
Validation identifies the invalid field rather than reporting an accuracy error
for invalid latitude/longitude.

## GPS walk / ride references

After saving the map anchors and selecting your dog/group in live tracking,
open **Phone survey** → **GPS walk / ride survey**. Choose walking (up to
3 m/s) or a slow ride (up to 10 m/s), enter the maximum dog-to-phone distance,
and press **Start GPS survey** on the phone you are carrying. For example,
use 5 m only if the dogs stay within 5 m; stop if they run farther away.
This distance is an estimate you supply, not a Bermuda proximity measurement.

Allow precise browser location access. Use HTTPS; keep the page visible and
the screen on. BPS requests a screen wake lock where supported. No continuous
background Companion App location or notification permission is needed unless
you opt into the Android command described below.
GPS callbacks provide instantaneous fixes; the survey does not average a
moving route into one point or assume the dog stays at an earlier position.
Walks are generally better for examining range bias; rides also expose lag.

BPS saves one full diagnostic mark at most every 15 seconds. A fix must be
less than three seconds old and report accuracy of 20 m or better; GPS jumps
or reported speeds above the mode's limit pause capture. These limits are
quality gates, not guarantees of true GPS accuracy. The server rechecks the
fix and projects it against the alignment frozen with the BLE snapshot.
The location allowance includes reported GPS accuracy, anchor error/fit and
extrapolation uncertainty, your dog-to-phone allowance, and possible movement
during GPS age. Map and GPS timestamps, raw GPS coordinates, speed/heading
when supplied, session identity and the full settings/Bermuda evidence remain
in the private captured report. BLE packets and solved positions retain
their own timestamps; filtering may represent earlier positions.

Sessions stop after 30 minutes or 120 confirmed snapshots, on Stop, when the
page is hidden, live tracking ends, a save fails or the map/target changes.
Collection resumes only when you start another session. Closing the screen
therefore does not silently create points later. Save failures are explicit;
no offline queue or interpolation invents missing observations. A request
already saving when stopped may finish. Retained points are included in the
usual actual-location history and full ZIP download, subject to its storage
limits, so they can be downloaded from another device.
There is no need to download after each survey. Every confirmed capture saves
immediately in Home Assistant, and the combined download includes the retained
captures from several surveys. Capacity or age limits can remove older points;
the page reports retained and evicted counts.

Moving GPS labels are approximate phone-reference areas. They do not alter
calibration, receiver positions, heights or map alignment, and do not enter
stationary radio-model trend summaries. They are useful for identifying large
errors, group recovery and lag; use stationary known-location captures to
validate precise RSSI/attenuation corrections. Three or more independently
checked anchors give a better alignment check than two points that fit exactly.

### Android Companion App high accuracy

In the walk survey, enable **Request HA high accuracy during survey (Android)**,
press **Load phones**, and select your own `notify.mobile_app_…` service. An
administrator is required for these phone commands. Starting the walk requests
`command_high_accuracy_mode` / `turn_on`; stopping requests `turn_off`. The Android
Companion App must already have precise/background location permissions and
background location enabled. BPS cannot grant phone permissions. These requests
can increase Companion App location reporting to HA; the walk's reference labels
continue to use original browser GPS fixes and their independent quality checks.

The command is an explicit ON/OFF action. BPS cannot read or restore the app's
previous high-accuracy preferences, so leave it unchecked if you want existing
phone settings left alone. It does not change the phone's configured update
interval. A notification-service acknowledgment confirms a request, not delivery
or actual phone state, and neither high-accuracy mode nor averaging guarantees
one-metre accuracy. Unsupported or failed commands do not prevent browser GPS
collection.

The server keeps an expiring request, refreshed only while the visible survey
runs, and requests OFF after 90 seconds without renewal or 30 minutes total.
Closing/refreshing the page therefore does not rely on its last network request
for cleanup. OFF intents persist before ON is sent; failed cleanup is retried,
including after HA restarts. BPS also requests OFF during integration unload
and HA shutdown. Network/phone availability can still prevent delivery: use
**Turn off HA high accuracy** to retry cleanup for your BPS requests, or disable
high accuracy directly in the Companion App. No saved BLE survey data is removed.

The Android command implementation is documented in the Companion App source:
[notification commands](https://github.com/home-assistant/android/blob/main/app/src/main/kotlin/io/homeassistant/companion/android/notifications/MessagingManager.kt)
and [location mode handling](https://github.com/home-assistant/android/blob/main/app/src/full/kotlin/io/homeassistant/companion/android/sensors/LocationSensorManager.kt).
