# Phone receiver height survey

Open **Map & Setup → Phone receiver height survey** on a saved floor. This
optional tool is independent of GPS map anchors. Receiver `height` and tracker
heights use **metres above one shared floor/property reference**, within the
existing receiver range of 0–10 m. Choose that physical reference first; GPS
altitude above sea level is never saved as receiver height.

1. Press **Check phone barometers**. BPS lists up to 128 registry-proven Home
   Assistant Companion App (`mobile_app`) pressure sensors, including supported
   disabled/stale sensors. Select **your own** sensor explicitly. Enable the
   Companion App pressure sensor if your phone supports it; an empty inventory
   cannot prove the phone lacks a barometer. Pressure units hPa, mbar, Pa and kPa
   are converted to hPa. No family member's sensor is selected automatically.
2. If pressure is unavailable, explicitly use **Browser altitude**. Capturing
   requests this browser's location permission; HTTPS and device permission are
   normally required. Altitude and finite `altitudeAccuracy` are required.
   Horizontal GPS accuracy cannot qualify a height reading. This version does
   not use Home Assistant location entities as altitude sources.
3. Select **Metres** or **Feet**, enter the **known phone height above the common
   floor** at a reference point, then press **Capture common-floor reference**.
   Measure that height; do not assume a hand-held phone is at floor level.
   Keep the phone stationary for 30–45 seconds. Capture stops after 45 seconds,
   or press **Stop capture** after 30 seconds.
4. Visit a placed receiver in the same session. Select it, enter the signed
   **receiver above phone** offset in the chosen units (positive above, negative
   below, zero if level), then **Capture receiver height** while stationary.
   Review the preview before **Confirm receiver height** overwrites its height.
   Repeat for other placed receivers, then **Save Floor Plan**. New receivers
   should be placed with the existing picker first. Manual Receiver properties
   remain available whenever phone data is insufficient.

The estimate is `known reference phone height + measured vertical delta +
receiver-above-phone offset`. The exact selected phone pressure entity or this
browser supplies both captures; sources never mix. Pressure delta uses
`44330 × (1 − (station_pressure / reference_pressure)^0.1903)` metres; browser
delta subtracts the two altitudes so their common datum cancels. Heights are
saved in metres even when the inputs use feet; the preview reports metres. This survey changes no
radio correction or solver setting. Recalibrate after changing mount heights.

Each capture requires at least two distinct fresh measurements spanning 20
seconds. Pre-capture, stale, noisy and observed moving readings are rejected.
Repeated HA pressure values are deduplicated: publication updates for unrelated
attributes are not new pressure evidence. HA state publication age is computed
on the server and translated at the browser request start, including request
delay; **raw phone measurement age remains unknown**. Slow publication or
unchanging values may provide insufficient evidence; use manual measurement.

Both reported GPS vertical accuracy and observed spread remain in the
conservative combined uncertainty; repeated correlated fixes do not earn
`sqrt(N)` precision. Combined GPS vertical uncertainty over 2 m is blocked.
Pressure retains observed spread and a 0.5 m precision allowance per capture,
but **pressure precision is not proven absolute height accuracy**. Stable
pressure can still be biased by weather, temperature, wind or ventilation.
The preview shows reference age; recheck the reference when conditions change.

The reference expires 15 minutes after its capture starts. Floor/map changes,
reference/source/unit changes, cancellation and leaving the page clear it.
Return to the reference and recapture for a later visit. Changing the receiver
or offset requires a fresh station capture. Replaced, moved or edited receivers
cannot accept an earlier preview. Confirmation retains the reference for the
remaining receivers in the current session; source requests stop on capture
exit and time out after 10 seconds.

Only confirmed, bounded survey summaries persist with the receiver in private
Home Assistant storage. Manually changing a height clears its obsolete survey
provenance. Full local diagnostic downloads contain saved/staged provenance,
the selected source, reference/session age, sample counts, capture spans,
reported vertical accuracy, pressure/altitude spread, known phone height and
signed offset. They contain summary values rather than raw sample series;
inspect private sensor IDs and measurements before sharing a download.
