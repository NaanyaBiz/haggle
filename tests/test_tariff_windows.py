"""Unit tests for custom_components/haggle/tariff_windows.py (#141).

Pure logic only: the text grammar, validation, the compiled
(weekday, weekend) table, classification in the contract zone, the
fingerprint, the rate-sensor set and the implied-price profile.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from custom_components.haggle.tariff_windows import (
    SLOTS_PER_DAY,
    WindowError,
    band_of,
    band_profile,
    build_config,
    compile_table,
    configured_bands,
    desired_rate_bands,
    fingerprint,
    parse_ranges,
    render_ranges,
    validate,
)

SYD = ZoneInfo("Australia/Sydney")
BNE = ZoneInfo("Australia/Brisbane")
ADL = ZoneInfo("Australia/Adelaide")


def _cfg(windows: list[tuple[str, str, str, str]], residual: str = "offpeak", **kw):
    raw = {
        "version": 1,
        "mode": "windows",
        "residual": residual,
        "windows": [
            {"band": b, "start": s, "end": e, "days": d} for b, s, e, d in windows
        ],
        **kw,
    }
    config, error = validate(raw)
    assert error is None, error
    assert config is not None
    return config


# The #126 Ausgrid bill: peak 15-21, shoulder 10-14, off-peak the rest.
BILL_126 = [
    ("peak", "15:00", "21:00", "all"),
    ("shoulder", "10:00", "14:00", "all"),
]


def _local(day: tuple[int, int, int], hh: int, mm: int, tz: ZoneInfo) -> datetime:
    return datetime(*day, hh, mm, tzinfo=tz).astimezone(UTC)


# --- grammar ---------------------------------------------------------------


class TestGrammar:
    def test_single_range(self) -> None:
        assert parse_ranges("15:00-21:00", "peak") == [
            {"band": "peak", "start": "15:00", "end": "21:00", "days": "all"}
        ]

    def test_multiple_ranges_and_whitespace(self) -> None:
        out = parse_ranges("  07:00 - 09:00 ,17:00-20:00  weekdays ", "peak")
        assert [(w["start"], w["end"], w["days"]) for w in out] == [
            ("07:00", "09:00", "all"),
            ("17:00", "20:00", "weekdays"),
        ]

    def test_days_suffix_case_insensitive(self) -> None:
        assert parse_ranges("10:00-14:00 WeekEnds", "shoulder")[0]["days"] == (
            "weekends"
        )

    @pytest.mark.parametrize("end", ["24:00", "00:00"])
    def test_end_of_day_spellings(self, end: str) -> None:
        assert parse_ranges(f"22:00-{end}", "offpeak")[0]["end"] == "24:00"

    def test_empty_text_is_no_windows(self) -> None:
        assert parse_ranges("", "peak") == []
        assert parse_ranges(" , ", "peak") == []

    @pytest.mark.parametrize(
        ("text", "code"),
        [
            ("15:15-21:00", "window_not_half_hour"),
            ("15:00-21:45", "window_not_half_hour"),
            ("15:00-15:00", "window_empty"),
            ("00:00-00:00", "window_empty"),
            ("3pm-9pm", "invalid_window_format"),
            ("15:00", "invalid_window_format"),
            ("25:00-26:00", "invalid_window_format"),
            ("24:00-06:00", "invalid_window_format"),
            ("15:00-21:00 sundays", "invalid_window_format"),
            ("15:60-16:00", "invalid_window_format"),
            ("x" * 201, "invalid_window_format"),
            (", ".join(["01:00-01:30"] * 7), "too_many_windows"),
        ],
    )
    def test_errors(self, text: str, code: str) -> None:
        with pytest.raises(WindowError) as err:
            parse_ranges(text, "peak")
        assert err.value.code == code
        # The error carries the band, never the raw text.
        assert err.value.bands == ("peak",)
        assert text not in str(err.value)

    def test_six_ranges_allowed(self) -> None:
        text = ", ".join(f"{h:02d}:00-{h:02d}:30" for h in range(6))
        assert len(parse_ranges(text, "peak")) == 6

    @pytest.mark.parametrize(
        "text",
        [
            "15:00-21:00",
            "22:00-07:00",
            "07:00-09:00 weekdays, 17:00-20:00 weekdays",
            "00:00-24:00 weekends",
        ],
    )
    def test_render_parse_fixed_point(self, text: str) -> None:
        once = render_ranges(parse_ranges(text, "peak"), "peak")
        assert once == text
        assert render_ranges(parse_ranges(once, "peak"), "peak") == once

    def test_render_filters_by_band(self) -> None:
        config = _cfg(BILL_126)
        assert render_ranges(config["windows"], "peak") == "15:00-21:00"
        assert render_ranges(config["windows"], "shoulder") == "10:00-14:00"
        assert render_ranges(config["windows"], "superoffpeak") == ""


# --- validation --------------------------------------------------------------


class TestValidate:
    def test_oversized_window_list_rejected_before_any_item_is_read(self) -> None:
        """Security P3: a hand-edited list of 10,000 windows fails safe to
        invalid_config on its length alone — no item is inspected."""

        class _Untouchable(dict):
            def get(self, *_a, **_k):  # pragma: no cover - must never run
                raise AssertionError("item read before the length guard")

        item = _Untouchable(band="peak", start="15:00", end="21:00", days="all")
        raw = {
            "version": 1,
            "mode": "windows",
            "residual": "offpeak",
            "windows": [item] * 10_000,
        }
        assert validate(raw) == (None, "invalid_config")

    def test_flat(self) -> None:
        assert validate({"version": 1, "mode": "flat", "rates": {"peak": 9}}) == (
            {"version": 1, "mode": "flat"},
            None,
        )

    @pytest.mark.parametrize(
        ("raw", "code"),
        [
            (None, "invalid_config"),
            ("windows", "invalid_config"),
            ({"version": 2, "mode": "flat"}, "unsupported_version"),
            ({"mode": "flat"}, "unsupported_version"),
            ({"version": 1, "mode": "clock"}, "invalid_config"),
            (
                {"version": 1, "mode": "windows", "residual": "peak", "windows": []},
                "invalid_config",
            ),
            (
                {"version": 1, "mode": "windows", "residual": "offpeak"},
                "invalid_config",
            ),
            (
                {
                    "version": 1,
                    "mode": "windows",
                    "residual": "offpeak",
                    "windows": [],
                },
                "no_windows",
            ),
            (
                {
                    "version": 1,
                    "mode": "windows",
                    "residual": "offpeak",
                    "windows": [
                        {"band": "ev", "start": "00:00", "end": "06:00", "days": "all"}
                    ],
                },
                "invalid_config",
            ),
            (
                {
                    "version": 1,
                    "mode": "windows",
                    "residual": "offpeak",
                    "windows": [
                        {
                            "band": "peak",
                            "start": "15:00",
                            "end": "21:00",
                            "days": "holidays",
                        }
                    ],
                },
                "invalid_window_format",
            ),
            (
                {
                    "version": 1,
                    "mode": "windows",
                    "residual": "offpeak",
                    "windows": ["15:00-21:00"],
                },
                "invalid_config",
            ),
            (
                {
                    "version": 1,
                    "mode": "windows",
                    "residual": "offpeak",
                    "windows": [
                        {"band": "peak", "start": 15, "end": "21:00", "days": "all"}
                    ],
                },
                "invalid_config",
            ),
        ],
    )
    def test_invalid_configs_fail_safe(self, raw: object, code: str) -> None:
        assert validate(raw) == (None, code)

    @pytest.mark.parametrize(
        "rates",
        [
            {"peak": 0.0099},
            {"peak": 5.01},
            {"peak": 44.17},  # c/kWh typed where AUD/kWh is stored
            {"peak": float("nan")},
            {"peak": float("inf")},
            {"peak": "0.4"},
        ],
    )
    def test_rates_out_of_range(self, rates: dict) -> None:
        raw = {
            "version": 1,
            "mode": "windows",
            "residual": "offpeak",
            "windows": [
                {"band": "peak", "start": "15:00", "end": "21:00", "days": "all"}
            ],
            "rates": rates,
        }
        assert validate(raw) == (None, "rate_out_of_range")

    @pytest.mark.parametrize("rates", [{"normal": 0.3}, {"peak": True}, [0.3]])
    def test_rates_malformed(self, rates: object) -> None:
        raw = {
            "version": 1,
            "mode": "windows",
            "residual": "offpeak",
            "windows": [
                {"band": "peak", "start": "15:00", "end": "21:00", "days": "all"}
            ],
            "rates": rates,
        }
        assert validate(raw) == (None, "invalid_config")

    def test_rate_bounds_inclusive(self) -> None:
        config = _cfg(BILL_126, rates={"peak": 5.0, "shoulder": 0.01})
        assert config["rates"] == {"peak": 5.0, "shoulder": 0.01}

    def test_canonical_is_sorted_and_json_safe(self) -> None:
        config = _cfg(
            [
                ("superoffpeak", "11:00", "14:00", "all"),
                ("peak", "17:00", "20:00", "weekdays"),
                ("peak", "07:00", "09:00", "weekdays"),
            ]
        )
        assert [(w["band"], w["start"]) for w in config["windows"]] == [
            ("peak", "07:00"),
            ("peak", "17:00"),
            ("superoffpeak", "11:00"),
        ]
        assert json.loads(json.dumps(config)) == config
        assert validate(config) == (config, None)

    @pytest.mark.parametrize(
        ("windows", "bands"),
        [
            (
                [
                    ("peak", "15:00", "21:00", "all"),
                    ("shoulder", "20:00", "22:00", "all"),
                ],
                ("peak", "shoulder"),
            ),
            (
                [("peak", "15:00", "21:00", "all"), ("peak", "16:00", "17:00", "all")],
                ("peak", "peak"),
            ),
            (
                # a wrapping window overlaps an early-morning one
                [
                    ("offpeak", "22:00", "07:00", "all"),
                    ("superoffpeak", "00:00", "06:00", "weekdays"),
                ],
                ("offpeak", "superoffpeak"),
            ),
            (
                # "all" overlaps a weekend-only window
                [
                    ("shoulder", "10:00", "14:00", "weekends"),
                    ("peak", "13:00", "15:00", "all"),
                ],
                ("peak", "shoulder"),
            ),
        ],
    )
    def test_overlaps_rejected_naming_both_bands(self, windows, bands) -> None:
        raw = {
            "version": 1,
            "mode": "windows",
            "residual": "offpeak",
            "windows": [
                {"band": b, "start": s, "end": e, "days": d} for b, s, e, d in windows
            ],
        }
        config, err = validate(raw)
        assert (config, err) == (None, "windows_overlap")
        _, errors, named = build_config(
            "windows",
            "offpeak",
            {
                b: ", ".join(f"{s}-{e} {d}" for bb, s, e, d in windows if bb == b)
                for b in {w[0] for w in windows}
            },
            {},
        )
        assert errors == {"base": "windows_overlap"}
        assert sorted(named) == sorted(bands)

    def test_weekday_and_weekend_windows_may_share_hours(self) -> None:
        config = _cfg(
            [
                ("peak", "15:00", "21:00", "weekdays"),
                ("shoulder", "15:00", "21:00", "weekends"),
            ]
        )
        weekday, weekend = compile_table(config)
        assert weekday[30] == "peak"
        assert weekend[30] == "shoulder"

    def test_adjacent_windows_do_not_overlap(self) -> None:
        # end is exclusive: 14:00-15:00 then 15:00-21:00 share no slot.
        config = _cfg(
            [("shoulder", "14:00", "15:00", "all"), ("peak", "15:00", "21:00", "all")]
        )
        weekday, _ = compile_table(config)
        assert weekday[27] == "offpeak"
        assert weekday[28] == weekday[29] == "shoulder"
        assert weekday[30] == weekday[41] == "peak"
        assert weekday[42] == "offpeak"


class TestBuildConfig:
    def test_flat(self) -> None:
        assert build_config("flat", "offpeak", {"peak": "garbage"}, {}) == (
            {"version": 1, "mode": "flat"},
            {},
            (),
        )

    def test_rates_converted_from_cents(self) -> None:
        config, errors, _ = build_config(
            "windows", "normal", {"superoffpeak": "00:00-06:00"}, {"superoffpeak": 8.5}
        )
        assert errors == {}
        assert config is not None
        assert config["rates"] == {"superoffpeak": 0.085}
        assert config["residual"] == "normal"

    @pytest.mark.parametrize("cents", [0.5, 500.01, 1000.0])
    def test_rate_bounds(self, cents: float) -> None:
        _, errors, _ = build_config(
            "windows", "offpeak", {"peak": "15:00-21:00"}, {"peak": cents}
        )
        assert errors == {"peak_rate": "rate_out_of_range"}

    def test_per_band_error_keys(self) -> None:
        config, errors, _ = build_config(
            "windows",
            "offpeak",
            {"peak": "15:15-21:00", "shoulder": "nonsense", "offpeak": ""},
            {},
        )
        assert config is None
        assert errors == {
            "peak": "window_not_half_hour",
            "shoulder": "invalid_window_format",
        }

    def test_no_windows(self) -> None:
        assert build_config("windows", "offpeak", {}, {}) == (
            None,
            {"base": "no_windows"},
            (),
        )


# --- classification ------------------------------------------------------------


class TestClassification:
    def test_bill_126_all_48_slots(self) -> None:
        weekday, weekend = compile_table(_cfg(BILL_126))
        expected = (
            ["offpeak"] * 20 + ["shoulder"] * 8 + ["offpeak"] * 2 + ["peak"] * 12
        ) + ["offpeak"] * 6
        assert len(expected) == SLOTS_PER_DAY
        assert list(weekday) == expected
        assert list(weekend) == expected

    def test_midnight_wrap(self) -> None:
        weekday, _ = compile_table(
            _cfg([("offpeak", "22:00", "07:00", "all")], residual="normal")
        )
        assert list(weekday) == ["offpeak"] * 14 + ["normal"] * 30 + ["offpeak"] * 4

    def test_ev_plan_residual_anytime(self) -> None:
        config = _cfg([("superoffpeak", "00:00", "06:00", "all")], residual="normal")
        weekday, weekend = compile_table(config)
        assert list(weekday) == ["superoffpeak"] * 12 + ["normal"] * 36
        assert weekday == weekend
        assert configured_bands(config) == {"superoffpeak", "normal"}

    def test_residual_not_configured_when_fully_covered(self) -> None:
        config = _cfg(
            [("peak", "00:00", "12:00", "all"), ("shoulder", "12:00", "24:00", "all")]
        )
        assert configured_bands(config) == {"peak", "shoulder"}

    def test_band_of_uses_contract_local_wall_clock(self) -> None:
        table = compile_table(_cfg(BILL_126))
        # 15:00 AEDT (Sydney, summer) = 04:00Z: peak in Sydney, but the SAME
        # instant is 14:00 in Brisbane (shoulder) and 14:30 in Adelaide.
        instant = _local((2027, 1, 12), 15, 0, SYD)
        assert instant == datetime(2027, 1, 12, 4, tzinfo=UTC)
        assert band_of(instant, SYD, table) == "peak"
        assert band_of(instant, BNE, table) == "offpeak"  # 14:00 BNE
        assert band_of(instant - timedelta(minutes=30), BNE, table) == "shoulder"
        assert band_of(instant, ADL, table) == "offpeak"  # 14:30 ACDT
        assert band_of(instant, ZoneInfo("UTC"), table) == "offpeak"  # 04:00

    def test_end_exclusive_start_inclusive(self) -> None:
        table = compile_table(_cfg(BILL_126))
        day = (2027, 1, 12)
        assert band_of(_local(day, 14, 30, SYD), SYD, table) == "offpeak"
        assert band_of(_local(day, 15, 0, SYD), SYD, table) == "peak"
        assert band_of(_local(day, 20, 30, SYD), SYD, table) == "peak"
        assert band_of(_local(day, 21, 0, SYD), SYD, table) == "offpeak"

    def test_weekday_weekend_uses_local_day(self) -> None:
        table = compile_table(
            _cfg([("peak", "07:00", "09:00", "weekdays")], residual="offpeak")
        )
        # Monday 2027-01-11 07:00 AEDT = Sunday 20:00Z: the LOCAL day decides.
        monday = _local((2027, 1, 11), 7, 0, SYD)
        assert monday.weekday() == 6  # Sunday in UTC
        assert band_of(monday, SYD, table) == "peak"
        saturday = _local((2027, 1, 9), 7, 0, SYD)
        assert band_of(saturday, SYD, table) == "offpeak"
        friday = _local((2027, 1, 8), 8, 30, SYD)
        assert band_of(friday, SYD, table) == "peak"

    def test_wrapping_weekday_window_is_per_local_day(self) -> None:
        table = compile_table(
            _cfg([("offpeak", "22:00", "07:00", "weekdays")], residual="normal")
        )
        # Friday 23:00 is a weekday slot; Saturday 01:00 is not.
        assert band_of(_local((2027, 1, 8), 23, 0, SYD), SYD, table) == "offpeak"
        assert band_of(_local((2027, 1, 9), 1, 0, SYD), SYD, table) == "normal"
        assert band_of(_local((2027, 1, 11), 1, 0, SYD), SYD, table) == "offpeak"

    def test_dst_fall_back_both_folds_share_the_band(self) -> None:
        # Sydney 2027-04-04: 03:00 AEDT -> 02:00 AEST; 02:00-03:00 occurs twice.
        table = compile_table(
            _cfg([("superoffpeak", "02:00", "03:00", "all")], residual="normal")
        )
        first = datetime(2027, 4, 3, 15, 0, tzinfo=UTC)  # 02:00 AEDT (fold 0)
        second = datetime(2027, 4, 3, 16, 0, tzinfo=UTC)  # 02:00 AEST (fold 1)
        assert first.astimezone(SYD).hour == second.astimezone(SYD).hour == 2
        for dt in (first, second, first + timedelta(minutes=30)):
            assert band_of(dt, SYD, table) == "superoffpeak"
        assert band_of(datetime(2027, 4, 3, 17, 0, tzinfo=UTC), SYD, table) == "normal"

    def test_dst_spring_forward_skipped_hour(self) -> None:
        # Sydney 2026-10-04: 02:00 AEST -> 03:00 AEDT; 02:xx never exists.
        table = compile_table(
            _cfg([("peak", "02:00", "03:00", "all")], residual="offpeak")
        )
        day_start = datetime(2026, 10, 4, tzinfo=SYD).astimezone(UTC)
        bands = [
            band_of(day_start + timedelta(minutes=30 * i), SYD, table)
            for i in range(46)
        ]
        assert "peak" not in bands
        assert len(bands) == 46


# --- fingerprint / rate sensors ------------------------------------------------


class TestFingerprintAndRates:
    def test_fingerprint_stable_and_ignores_rates(self) -> None:
        a = _cfg(BILL_126, rates={"peak": 0.44})
        b = _cfg(BILL_126, rates={"peak": 0.50, "shoulder": 0.3})
        assert fingerprint(a) == fingerprint(b)
        assert fingerprint(a) == fingerprint(json.loads(json.dumps(a)))
        assert len(fingerprint(a)) == 12

    def test_fingerprint_changes_with_attribution(self) -> None:
        a = _cfg(BILL_126)
        assert fingerprint(a) != fingerprint(_cfg(BILL_126, residual="normal"))
        assert fingerprint(a) != fingerprint(_cfg([("peak", "16:00", "20:00", "all")]))
        assert fingerprint(a) != fingerprint({"version": 1, "mode": "flat"})

    def test_desired_rate_bands(self) -> None:
        # legacy: v0.5.0's active set among peak/offpeak/shoulder only
        assert desired_rate_bands(None, {"peak", "normal"}) == {"peak"}
        assert desired_rate_bands(None, set()) == frozenset()
        # flat: none
        assert desired_rate_bands({"version": 1, "mode": "flat"}, {"peak"}) == set()
        # windows: configured rate bands (normal is covered by unit_rate)
        ev = _cfg([("superoffpeak", "00:00", "06:00", "all")], residual="normal")
        assert desired_rate_bands(ev, {"peak"}) == {"superoffpeak"}
        assert desired_rate_bands(_cfg(BILL_126), set()) == {
            "peak",
            "shoulder",
            "offpeak",
        }


# --- implied-price profile ------------------------------------------------------


class TestBandProfile:
    def _reading(self, hh: int, mm: int, kwh: float, cost: float, band: str):
        return SimpleNamespace(
            dt=_local((2027, 1, 12), hh, mm, SYD),
            kwh=kwh,
            cost_aud=cost,
            rate_type=band,
        )

    def test_profile_shape_rounding_and_coarse_counts(self) -> None:
        readings = [self._reading(15, 0, 1.234, 0.545115, "peak") for _ in range(5)]
        readings += [self._reading(3, 30, 0.5, 0.1, "offpeak")]
        readings += [self._reading(4, 0, 0.004, 0.01, "offpeak")]  # below floor
        profile = band_profile(readings, {"normal": 6, "peak": 1}, SYD, {"peak": 0.44})
        slots = profile["slots"]
        assert len(slots) == 48
        assert slots[30] == {
            "slot": "15:00",
            "band": "peak",
            "n": "4+",
            "median_c_per_kwh": 44.17,
        }
        assert slots[7] == {
            "slot": "03:30",
            "band": "offpeak",
            "n": "1-3",
            "median_c_per_kwh": 20.0,
        }
        assert slots[8] == {
            "slot": "04:00",
            "band": None,
            "n": "0",
            "median_c_per_kwh": None,
        }
        assert profile["bands"]["peak"] == {
            "n": 5,
            "p10": 44.17,
            "median": 44.17,
            "p90": 44.17,
            "configured_rate_c_per_kwh": 44.0,
        }
        assert profile["bands"]["offpeak"]["configured_rate_c_per_kwh"] is None
        assert profile["raw_type_counts"] == {"normal": 6, "peak": 1}

    def test_profile_never_carries_kwh(self) -> None:
        readings = [self._reading(12, 0, 1.2345, 0.37, "normal")]
        blob = json.dumps(band_profile(readings, {}, SYD, {}))
        assert "1.2345" not in blob
        assert "0.37" not in blob
        assert "2027" not in blob

    def test_percentiles(self) -> None:
        readings = [
            self._reading(12, 0, 1.0, c / 100.0, "normal") for c in range(10, 110, 10)
        ]
        band = band_profile(readings, {}, SYD, {})["bands"]["normal"]
        # Nearest-rank on the sorted list (index round(f * (n - 1))).
        assert (band["p10"], band["median"], band["p90"]) == (20.0, 50.0, 90.0)
        assert band["n"] == 10
