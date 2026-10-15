# Expanding a local OpenCode tracking monitor

Keep data and reports on your Home Assistant host. The monitor uses authenticated
read-only APIs; the following prompt does not authorize calibration or layout
changes. Match all endpoints and fields against the installed integration version.

```text
Expand the existing local BPS/Bermuda monitor using the data already collected.
Keep it stopped while implementing and testing. Never publish my coordinates,
diagnostics, tokens or identifiers to GitHub or external services.

1. Add event windows around each group jump: preserve at least 30 seconds before
   and after, with group/member coordinates, source observation timestamps,
   receiver participation, anchor selection, weights, uncertainty and motion-gate
   decisions. Distinguish fresh evidence from repeated cached observations. A
   movement measured over two five-second polls is not a single solver step.

2. Capture displayed circle radius separately from raw estimated uncertainty and
   the receiver-based display limit. Server uncertainty alone does not establish
   that a 100-metre circle was actually drawn by the browser.

3. Discover GET /api/bps/ground_truth and its download=1 export when supported.
   Fetch new actual-location marks and retain their frozen snapshots. Labels
   apply only to their capture instant; never extend them before or after it.
   Use recorded group membership, not today's settings. Report the age of each
   scored fix and leave missing/stale/wrong-floor positions unscored.

4. At labelled locations, compare expected receiver slant range with both raw
   Bermuda and corrected BPS range. Retain height assumptions, units, source
   identity, effective RSSI offset, reference power and attenuation. Rank station
   disagreements across multiple beacons and locations. A range residual against
   an unlabelled solved position is not measured receiver error.

5. For calibration suggestions, separate receiver-wide bias, beacon-wide bias,
   mapped obstructions, weak reception and map/height/scale errors. Use held-out
   labelled locations for validation. Do not recommend numerical RSSI changes
   without a verified effective model and enough independent observations.

6. Track unique packet reception cadence and gaps for each instance-scoped
   beacon/receiver pair. Scanner activity and hearing a specific beacon are
   separate. Preserve MAC/instance identities and original source clocks.

7. Include effective beacon hardware profiles and configured transmit power when
   available. Model names and 'maximum TX' are descriptive until the actual dBm
   setting/reference power is verified; do not invent antenna or radio values.

8. Compare paired-beacon fusion with its individual members on the same labelled
   observations. Report group error, each member's error, selected member, motion
   direction and source age. Distinguish an isolated outlier, loss of the better
   member and coherent real movement. Analyse uncertainty changes alongside
   geometry, effective receiver count and member disagreement.

9. Include phone-survey context when supported: BLE proximity evidence, GPS
   source, sample count/span, average, spread, map-anchor accuracy and whether the
   proposed station placement was verified. A location-update request does not
   prove the phone returned a new GPS measurement.

10. When available, use the private diagnostic ZIP's radio_survey.json and join
    each capture ID to actual_location_history.json. Compare raw-model, filtered
    and corrected range errors in measured-distance bands, separated by receiver
    and beacon hardware, antenna metadata, model, map and environment. Account
    for exclusions, sample limits, repeated packets and missing older radio data.
    Recent packet history is unlabelled context, never extra truth observations.
    Report how often actual distance falls outside signal-variation intervals;
    these intervals do not bound systematic bias. Compare repeated locations
    with experimental long-range interval positioning off and on, preserving the
    actual published solve mode/fallback and source ages rather than assuming
    the saved toggle proves which mode produced a position. Do not change it
    or any radio settings automatically.

Keep bounded local retention, single-flight collection, backoff and anomaly
cooldowns. Report only new or materially changed findings. For each finding show
the evidence, confidence, a small verification experiment, and validation needed
before changing settings. Add tests using synthetic data, then show a sample
report and exact start/stop commands.
```
