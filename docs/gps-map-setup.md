# GPS map alignment and receiver placement

Open **Map & Setup → GPS map alignment & receiver placement** on a saved floor.
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
   GPS** while standing at the known anchor or receiver. This button is the only
   action that requests browser location permission. Browser location generally
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
7. Press **Save Floor Plan** to persist anchors and staged receiver positions.

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
