"""Pure signal bands and opt-in position refinement."""

import copy
import math
from types import SimpleNamespace

import bps
import pytest
from bps import range_intervals as bands


def advert(**changes):
    raw_rssi = -79.0
    reference = -59.0
    attenuation = 2.0
    raw_distance = 10 ** ((reference - raw_rssi) / (10 * attenuation))
    value = {"status": "current", "observed": 99.0, "observed_clock": "source_epoch",
             "stamp": 88.0, "age": 1.0, "raw_rssi": raw_rssi,
             "reference_power": reference, "rssi_offset": 0.0,
             "attenuation": attenuation, "raw_distance": raw_distance,
             "filtered_distance": 10.0, "model_version": "test-model",
             "quality": {"mode": "legacy", "status": "current", "age": 1.0,
                         "samples": 5, "noise_db": 2.0,
                         "last_valid_stamp": 87.0,
                         "last_valid_observed": 98.0,
                         "accepted": False}}
    value.update(changes)
    return value


def test_exponential_signal_band_is_asymmetric_and_preserves_provenance():
    source = advert()
    before = copy.deepcopy(source)
    result = bands.signal_interval(source, 100)
    assert source == before
    assert result["status"] == "available"
    assert result["solver_eligible"] is True
    assert result["source_observed"] == 99
    assert result["source_stamp"] == 88
    assert result["model_version"] == "test-model"
    assert result["noise_db"] == 2
    assert result["noise_floor_db"] == .5
    assert result["noise_multiplier"] == 2
    assert result["delta_db"] == 4
    assert result["center_m"] == pytest.approx(10)
    assert result["lower_m"] == pytest.approx(10 ** .8)
    assert result["upper_m"] == pytest.approx(10 ** 1.2)
    assert result["upper_m"] - 10 > 10 - result["lower_m"]
    assert "not a confidence interval" in result["meaning"]


def test_noise_floor_is_visible_and_short_range_stays_diagnostic():
    source = advert(raw_rssi=-65, raw_distance=10 ** .3,
                    quality={**advert()["quality"], "noise_db": .1})
    result = bands.signal_interval(source, 100)
    assert result["status"] == "available"
    assert result["center_m"] < 10
    assert result["delta_db"] == 1


@pytest.mark.parametrize("change,reason", [
    ({"status": "stale"}, "source_not_current"),
    ({"age": 3.01}, "raw_packet_stale"),
    ({"observed": 96.9}, "raw_packet_stale"),
    ({"observed": 101}, "source_clock_unavailable_or_future"),
    ({"stamp": None}, "source_clock_unavailable_or_future"),
    ({"quality": {**advert()["quality"], "samples": 4}}, "insufficient_quality_samples"),
    ({"quality": {**advert()["quality"], "noise_db": math.inf}}, "noise_unavailable"),
    ({"raw_distance": 20}, "radio_model_mismatch"),
    ({"attenuation": 0}, "invalid_radio_model"),
    ({"raw_rssi": math.nan}, "raw_rssi_unavailable"),
])
def test_invalid_stale_or_mismatching_raw_model_is_unavailable(change, reason):
    result = bands.signal_interval(advert(**change), 100)
    assert result["status"] == "unavailable"
    assert result["reason"] == reason
    assert result["solver_eligible"] is False


def test_rejected_pet_raw_packet_keeps_band_but_cannot_steer_solver():
    quality = {**advert()["quality"], "mode": "pet", "last_valid_stamp": 87.0}
    result = bands.signal_interval(advert(quality=quality), 100)
    assert result["status"] == "available"
    assert result["solver_eligible"] is False
    assert result["solver_reason"] == "raw_packet_not_accepted_by_filter"
    quality["last_valid_stamp"] = 88.0
    quality["last_valid_observed"] = 99.0
    eligible = bands.signal_interval(advert(quality=quality), 100)
    assert eligible["solver_eligible"] is True
    assert quality["accepted"] is False  # read-only snapshot flag has no veto


@pytest.mark.parametrize("change,reason", [
    ({"filtered_distance": None}, "filtered_distance_unavailable"),
    ({"quality": {**advert()["quality"], "status": "stale"}}, "filter_not_current"),
    ({"quality": {**advert()["quality"], "age": 4}}, "filter_clock_stale_or_unknown"),
])
def test_filtered_data_gates_solver_only(change, reason):
    result = bands.signal_interval(advert(**change), 100)
    assert result["status"] == "available"
    assert result["solver_eligible"] is False
    assert result["solver_reason"] == reason


def test_projection_monotonic_correction_and_height_clamp_without_radius_floor():
    interval = {"status": "available", "lower_m": 2.0, "center_m": 3.0, "upper_m": 4.0}
    before = copy.deepcopy(interval)
    result = bands.project_interval(interval, 2.0, 5.0, 10.0)
    assert interval == before
    assert result["status"] == "available"
    assert result["lower_m"] == 0
    assert result["lower_px"] == 0
    assert result["center_px"] == pytest.approx(10 * math.sqrt(36 - 25))
    assert result["upper_px"] == pytest.approx(10 * math.sqrt(64 - 25))
    assert result["lower_px"] <= result["center_px"] <= result["upper_px"]


@pytest.mark.parametrize("interval,factor,dz,scale,reason", [
    ({"status": "unavailable"}, 1, 0, 10, "source_interval_unavailable"),
    ({"status": "available", "lower_m": 2, "center_m": 1, "upper_m": 3},
     1, 0, 10, "invalid_slant_interval"),
    ({"status": "available", "lower_m": 1, "center_m": 2, "upper_m": 3},
     0, 0, 10, "invalid_projection_parameters"),
    ({"status": "available", "lower_m": 1, "center_m": 2, "upper_m": 3},
     1, math.inf, 10, "invalid_projection_parameters"),
])
def test_projection_rejects_bad_inputs(interval, factor, dz, scale, reason):
    assert bands.project_interval(interval, factor, dz, scale)["reason"] == reason


def test_no_band_passes_exact_solver_call_and_result_through():
    points = [(0, 0, 5, 1, 5), (10, 0, 5, 1, 5), (0, 10, 5, 1, 5)]
    sentinel = object()
    calls = []
    telemetry = {}

    def solver(actual, **kwargs):
        calls.append((actual, kwargs))
        return sentinel

    result = bands.solve_with_intervals(points, [None] * 3, bounds=(-1, -1, 20, 20),
                                        min_weight_radius=2, point_solver=solver,
                                        telemetry=telemetry)
    assert result is sentinel
    assert calls == [(points, {"bounds": (-1, -1, 20, 20), "min_weight_radius": 2})]
    assert telemetry == {"applied": False, "reason": "no_usable_intervals"}


def test_missing_point_seed_reports_fallback_reason():
    points = [(0, 0, 5, 1, 5), (10, 0, 5, 1, 5), (0, 10, 5, 1, 5)]
    telemetry = {}
    assert bands.solve_with_intervals(points, [(1, 9)] * 3,
                                      point_solver=lambda *_args, **_kw: None,
                                      telemetry=telemetry) is None
    assert telemetry == {"applied": False, "reason": "point_seed_failed"}


def test_invalid_band_outside_point_radius_uses_exact_point_row():
    points = [(0, 0, 5, 1, 5), (10, 0, 5, 1, 5), (0, 10, 5, 1, 5)]
    sentinel = (2.5, 2.5)
    result = bands.solve_with_intervals(points, [(20, 30), None, None],
                                        point_solver=lambda *_args, **_kw: sentinel)
    assert result is sentinel


def test_nearby_exact_receivers_and_one_biased_far_band_improve_held_out_truth():
    truth = (40.0, 35.0)
    receivers = [(0.0, 0.0), (80.0, 0.0), (0.0, 80.0), (300.0, 300.0)]
    actual = [math.dist(point, truth) for point in receivers]
    far_biased = actual[-1] + 100.0
    points = [(x, y, far_biased if index == 3 else actual[index], 1.0,
               far_biased if index == 3 else actual[index])
              for index, (x, y) in enumerate(receivers)]
    before = copy.deepcopy(points)
    ordinary = bps.trilaterate(points, bounds=(-10, -10, 400, 400))
    refined = bands.solve_with_intervals(points, [None, None, None,
                                                    (actual[-1] - 20, far_biased + 20)],
                                         bounds=(-10, -10, 400, 400),
                                         point_solver=bps.trilaterate)
    assert points == before
    assert math.dist(refined, truth) < math.dist(ordinary, truth)


def test_all_far_bands_remain_bounded_diagnostic_not_accuracy_claim():
    receivers = [(0, 0), (100, 0), (0, 100)]
    points = [(x, y, 100.0, 1.0, 100.0) for x, y in receivers]
    result = bands.solve_with_intervals(points, [(50, 150)] * 3,
                                        bounds=(0, 0, 100, 100),
                                        point_solver=bps.trilaterate)
    assert result is not None
    assert all(math.isfinite(value) for value in result)
    assert 0 <= result[0] <= 100 and 0 <= result[1] <= 100


def test_optimizer_exception_and_failure_return_original(monkeypatch):
    points = [(0, 0, 5, 1, 5), (10, 0, 5, 1, 5), (0, 10, 5, 1, 5)]
    original = (4.0, 4.0)
    def solver(*_args, **_kw):
        return original
    telemetry = {}
    monkeypatch.setattr(bands, "least_squares", lambda *_args, **_kw: (_ for _ in ()).throw(ValueError("bad")))
    assert bands.solve_with_intervals(points, [(1, 9)] * 3, point_solver=solver,
                                      telemetry=telemetry) is original
    assert telemetry == {"applied": False, "reason": "interval_solve_failed"}
    monkeypatch.setattr(bands, "least_squares", lambda *_args, **_kw: SimpleNamespace(
        success=False, x=(math.nan, math.nan)))
    assert bands.solve_with_intervals(points, [(1, 9)] * 3, point_solver=solver,
                                      telemetry=telemetry) is original
    assert telemetry == {"applied": False, "reason": "interval_solve_failed"}


def test_5_tuple_slant_weight_preserved_and_max_iterations_bounded(monkeypatch):
    points = [(0, 0, 5, .25, 20), (10, 0, 5, 1, 5), (0, 10, 5, 1, 5)]
    captured = {}
    telemetry = {}

    def fake_least_squares(objective, x0, **kwargs):
        captured.update(kwargs)
        captured["residual"] = objective((4, 4))
        captured["jacobian"] = kwargs["jac"]((4, 4))
        return SimpleNamespace(success=True, x=(4, 4))

    monkeypatch.setattr(bands, "least_squares", fake_least_squares)
    got = bands.solve_with_intervals(points, [(1, 9), None, None],
                                     point_solver=lambda *_args, **_kw: (3, 3),
                                     telemetry=telemetry)
    assert got == (4, 4)
    assert telemetry == {"applied": True, "reason": "applied"}
    assert captured["loss"] == "soft_l1" and captured["max_nfev"] == 200
    assert captured["residual"].shape == (4,)
    assert captured["jacobian"].shape == (4, 2)
    assert captured["residual"][-1] == pytest.approx(.05 * (.25 ** .5 / 20) *
                                                     (math.hypot(4, 4) - 5))
