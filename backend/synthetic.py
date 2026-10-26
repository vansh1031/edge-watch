"""
synthetic.py — Built-in synthetic sensor for Edge Watch
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Generates readings in the same format as the real wired RMS sensor so the
whole pipeline (ML → API → dashboard) works without hardware.

The generator walks through a repeating demo scenario (~24 min):
  normal → imbalance developing → misalignment fault → maintenance stop
  → normal → overheating drift → back to normal

Signals are built from:
  - a base profile blended between "normal" and fault profiles
  - slow load variation (sinusoid) + correlated drift (AR(1)) + white noise
  - thermal inertia on temperature
  - occasional acoustic transients

Profile medians are taken from sensor_data_labeled.csv.
"""

import math
import random
import time

PROFILES = {
    "normal":       {"aRMSx": 0.055, "aRMSy": 0.107, "aRMSz": 0.052, "vRMSx": 0.56, "vRMSy": 1.13, "vRMSz": 0.57, "temperature": 27.3, "aucausticRMS": 49,  "extra": 52},
    "bearing":      {"aRMSx": 0.95,  "aRMSy": 0.60,  "aRMSz": 1.00,  "vRMSx": 2.0,  "vRMSy": 1.9,  "vRMSz": 2.7,  "temperature": 31,   "aucausticRMS": 150, "extra": 163},
    "misalignment": {"aRMSx": 0.72,  "aRMSy": 0.50,  "aRMSz": 0.81,  "vRMSx": 2.2,  "vRMSy": 3.7,  "vRMSz": 5.1,  "temperature": 31.3, "aucausticRMS": 143, "extra": 150},
    "imbalance":    {"aRMSx": 0.30,  "aRMSy": 0.35,  "aRMSz": 0.25,  "vRMSx": 3.2,  "vRMSy": 4.8,  "vRMSz": 1.4,  "temperature": 29,   "aucausticRMS": 70,  "extra": 90},
    "overheating":  {"aRMSx": 0.08,  "aRMSy": 0.13,  "aRMSz": 0.07,  "vRMSx": 0.7,  "vRMSy": 1.3,  "vRMSz": 0.7,  "temperature": 72,   "aucausticRMS": 55,  "extra": 60},
    "off":          {"aRMSx": 0.01,  "aRMSy": 0.01,  "aRMSz": 0.01,  "vRMSx": 0.02, "vRMSy": 0.02, "vRMSz": 0.02, "temperature": 25,   "aucausticRMS": 34,  "extra": 44},
}

VIB_KEYS = ["aRMSx", "aRMSy", "aRMSz", "vRMSx", "vRMSy", "vRMSz"]
ALL_KEYS = VIB_KEYS + ["temperature", "aucausticRMS", "extra"]

# (label, steps, start_weights, end_weights)
# Weights blend fault profiles on top of "normal": base = normal + Σ w·(fault − normal)
# The trained model is very sensitive, so small weights already show up as
# warning (≈0.05–0.15) and critical (≥0.2).
SCENARIO = [
    ("Normal operation",       150, {},                                    {}),
    ("Imbalance developing",   120, {},                                    {"imbalance": 0.12}),
    ("Misalignment fault",      45, {"imbalance": 0.12},                   {"imbalance": 0.12, "misalignment": 0.5}),
    ("Misalignment fault",      30, {"imbalance": 0.12, "misalignment": 0.5}, {"imbalance": 0.12, "misalignment": 0.5}),
    ("Maintenance stop",        30, {"off": 1.0},                          {"off": 1.0}),
    ("Normal operation",       150, {},                                    {}),
    ("Overheating drift",       90, {},                                    {"overheating": 0.3}),
    ("Overheating drift",       30, {"overheating": 0.3},                  {"overheating": 0.3}),
    ("Cooling down",            60, {"overheating": 0.3},                  {}),
]
SCENARIO_STEPS = sum(p[1] for p in SCENARIO)

MAC = "SIM:00:00:00:00:01"


def _smoothstep(t: float) -> float:
    return t * t * (3 - 2 * t)


def _blend(weights: dict) -> dict:
    normal = PROFILES["normal"]
    out = dict(normal)
    for fault, w in weights.items():
        for k in ALL_KEYS:
            out[k] += w * (PROFILES[fault][k] - normal[k])
    return out


def _lerp_weights(a: dict, b: dict, t: float) -> dict:
    keys = set(a) | set(b)
    return {k: a.get(k, 0.0) + (b.get(k, 0.0) - a.get(k, 0.0)) * t for k in keys}


class SyntheticSensor:
    def __init__(self, interval_s: float = 2.0, scenario: str = "demo"):
        self.interval_s = interval_s
        self.scenario   = scenario          # "demo" (cycle) or "normal"
        self.step       = 0
        self.seq        = 0
        self._temp      = PROFILES["normal"]["temperature"]
        self._drift     = {k: 0.0 for k in ALL_KEYS}

    # ── Scenario position ─────────────────────────────────────
    def phase(self, step: int | None = None):
        """Return (label, weights) for the given scenario step."""
        if self.scenario == "normal":
            return "Normal operation", {}
        s = (self.step if step is None else step) % SCENARIO_STEPS
        for label, steps, w0, w1 in SCENARIO:
            if s < steps:
                return label, _lerp_weights(w0, w1, _smoothstep(s / max(steps - 1, 1)))
            s -= steps
        return "Normal operation", {}

    # ── Signal generation ─────────────────────────────────────
    def _reading(self, ts_ms: int, weights: dict) -> dict:
        base   = _blend(weights)
        t      = ts_ms / 1000.0
        load   = 1 + 0.04 * math.sin(2 * math.pi * t / 90) + 0.02 * math.sin(2 * math.pi * t / 23)
        motor_off = weights.get("off", 0) > 0.5

        reading = {}
        for k in VIB_KEYS + ["aucausticRMS", "extra"]:
            # AR(1) drift gives the slow wander real sensors have
            self._drift[k] = 0.92 * self._drift[k] + 0.02 * random.gauss(0, 1)
            noise = 1 + self._drift[k] + 0.03 * random.gauss(0, 1)
            val   = base[k] * noise * (1 if motor_off else load)
            reading[k] = round(max(val, 0.0), 6)

        # Occasional acoustic transient (impact / valve click)
        if not motor_off and random.random() < 0.02:
            reading["aucausticRMS"] = round(reading["aucausticRMS"] * random.uniform(1.08, 1.18), 6)

        # Temperature: thermal inertia toward target, slight load dependence
        target      = base["temperature"] + (0 if motor_off else 0.6 * (load - 1) * 10)
        self._temp += (target - self._temp) * 0.05 + 0.04 * random.gauss(0, 1)
        reading["temperature"] = round(self._temp, 5)

        self.seq += 1
        return {
            "timestamp": ts_ms,
            "seq":       self.seq,
            "key":       "RMS",
            **reading,
            "mac":       MAC,
            "simulated": True,
        }

    def next(self) -> dict:
        """Generate the next live reading (timestamp = now)."""
        label, weights = self.phase()
        r = self._reading(int(time.time() * 1000), weights)
        r["scenario"] = label
        self.step += 1
        return r

    def backfill(self, seconds: int) -> list[dict]:
        """Generate `seconds` of past normal-operation history ending now."""
        n      = int(seconds / self.interval_s)
        now_ms = int(time.time() * 1000)
        rows   = []
        for i in range(n, 0, -1):
            r = self._reading(now_ms - int(i * self.interval_s * 1000), {})
            r["scenario"] = "Normal operation"
            rows.append(r)
        return rows
