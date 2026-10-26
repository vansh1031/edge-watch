#!/usr/bin/env node
/**
 * sensor-simulator.js — Fake vibration/temperature sensor for Edge Watch
 * ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 * Publishes payloads in the same format as the real wired RMS sensor to
 * the MQTT topic the Flask backend subscribes to (wired/rms/#), so the
 * whole pipeline (backend → ML → dashboard) works without hardware.
 *
 * Usage:
 *   npm run simulate                          # healthy machine
 *   npm run simulate -- --mode bearing        # bearing fault
 *   npm run simulate -- --mode degrade        # healthy → slowly worsening fault
 *   npm run simulate -- --mode cycle          # rotates through all modes
 *   npm run simulate -- --interval 2000 --broker mqtt://localhost:1883
 *
 * Modes: normal | bearing | misalignment | imbalance | overheating | off | degrade | cycle
 *
 * Payloads carry "simulated": true and a SIM:* MAC address so the backend
 * never sends a real STOP command for simulated data, and simulated rows
 * are easy to filter out of sensor_data.csv.
 */

import mqtt from "mqtt";

// ─── CLI / env config ─────────────────────────────────────────
function arg(name, fallback) {
  const i = process.argv.indexOf(`--${name}`);
  if (i !== -1 && process.argv[i + 1]) return process.argv[i + 1];
  const env = process.env[`SIM_${name.toUpperCase()}`];
  return env ?? fallback;
}

const BROKER   = arg("broker", "mqtt://broker.emqx.io:1883");
const USERNAME = arg("username", "test");
const PASSWORD = arg("password", "test");
const TOPIC    = arg("topic", "wired/rms/SIM");
const MAC      = arg("mac", "SIM:00:00:00:00:01");
const INTERVAL = Number(arg("interval", 6000));   // real sensor sends every ~6 s
let   MODE     = arg("mode", "normal");

// ─── Sensor profiles (medians taken from sensor_data_labeled.csv) ──
const PROFILES = {
  normal:       { aRMSx: 0.055, aRMSy: 0.107, aRMSz: 0.052, vRMSx: 0.56, vRMSy: 1.13, vRMSz: 0.57, temperature: 27.3, aucausticRMS: 49,  extra: 52  },
  bearing:      { aRMSx: 0.95,  aRMSy: 0.60,  aRMSz: 1.00,  vRMSx: 2.0,  vRMSy: 1.9,  vRMSz: 2.7,  temperature: 31,   aucausticRMS: 150, extra: 163 },
  misalignment: { aRMSx: 0.72,  aRMSy: 0.50,  aRMSz: 0.81,  vRMSx: 2.2,  vRMSy: 3.7,  vRMSz: 5.1,  temperature: 31.3, aucausticRMS: 143, extra: 150 },
  imbalance:    { aRMSx: 0.30,  aRMSy: 0.35,  aRMSz: 0.25,  vRMSx: 3.2,  vRMSy: 4.8,  vRMSz: 1.4,  temperature: 29,   aucausticRMS: 70,  extra: 90  },
  overheating:  { aRMSx: 0.08,  aRMSy: 0.13,  aRMSz: 0.07,  vRMSx: 0.7,  vRMSy: 1.3,  vRMSz: 0.7,  temperature: 72,   aucausticRMS: 55,  extra: 60  },
  off:          { aRMSx: 0.01,  aRMSy: 0.01,  aRMSz: 0.01,  vRMSx: 0.02, vRMSy: 0.02, vRMSz: 0.02, temperature: 25,   aucausticRMS: 34,  extra: 44  },
};

const CYCLE_ORDER     = ["normal", "bearing", "normal", "misalignment", "normal", "imbalance", "normal", "overheating", "off"];
const CYCLE_STEPS     = 20;   // messages per mode in "cycle"
const DEGRADE_STEPS   = 150;  // messages to go from healthy to full fault in "degrade"
const DEGRADE_TARGET  = "bearing";

const VALID_MODES = [...Object.keys(PROFILES), "degrade", "cycle"];
if (!VALID_MODES.includes(MODE)) {
  console.error(`Unknown mode "${MODE}". Use one of: ${VALID_MODES.join(", ")}`);
  process.exit(1);
}

// ─── Signal generation ────────────────────────────────────────
// Gaussian noise (Box–Muller)
function gauss() {
  let u = 0, v = 0;
  while (u === 0) u = Math.random();
  while (v === 0) v = Math.random();
  return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
}

function lerpProfile(a, b, t) {
  const out = {};
  for (const k of Object.keys(a)) out[k] = a[k] + (b[k] - a[k]) * t;
  return out;
}

let seq = 0;
let smoothedTemp = PROFILES.normal.temperature;

function currentProfile() {
  if (MODE === "cycle") {
    const name = CYCLE_ORDER[Math.floor(seq / CYCLE_STEPS) % CYCLE_ORDER.length];
    return { name, base: PROFILES[name] };
  }
  if (MODE === "degrade") {
    const t = Math.min(seq / DEGRADE_STEPS, 1);
    return { name: `degrade ${(t * 100).toFixed(0)}%`, base: lerpProfile(PROFILES.normal, PROFILES[DEGRADE_TARGET], t * t) };
  }
  return { name: MODE, base: PROFILES[MODE] };
}

function round(v, d = 6) {
  return Number(Math.max(v, 0).toFixed(d));
}

function makeReading() {
  const { name, base } = currentProfile();
  const reading = {};
  for (const k of ["aRMSx", "aRMSy", "aRMSz", "vRMSx", "vRMSy", "vRMSz", "aucausticRMS", "extra"]) {
    reading[k] = round(base[k] * (1 + 0.05 * gauss()));   // ±5 % noise
  }
  // Temperature has thermal inertia — drift toward target instead of jumping
  smoothedTemp += (base.temperature - smoothedTemp) * 0.15 + 0.1 * gauss();
  reading.temperature = round(smoothedTemp, 5);

  return {
    payload: {
      timestamp: seq,          // real sensor sends its sequence number here
      key: "RMS",
      ...reading,
      mac: MAC,
      simulated: true,
    },
    name,
  };
}

// ─── MQTT ─────────────────────────────────────────────────────
const client = mqtt.connect(BROKER, {
  username: USERNAME,
  password: PASSWORD,
  clientId: `edgewatch-sim-${Math.random().toString(16).slice(2, 10)}`,
  reconnectPeriod: 3000,
});

let timer = null;

client.on("connect", () => {
  console.log(`[SIM] Connected to ${BROKER}`);
  console.log(`[SIM] Publishing to "${TOPIC}" every ${INTERVAL} ms — mode=${MODE}`);
  if (!timer) {
    publish();
    timer = setInterval(publish, INTERVAL);
  }
});

client.on("reconnect", () => console.log("[SIM] Reconnecting..."));
client.on("error", (err) => console.error(`[SIM] MQTT error: ${err.message}`));

function publish() {
  if (!client.connected) return;
  const { payload, name } = makeReading();
  client.publish(TOPIC, JSON.stringify(payload), { qos: 0 });
  console.log(
    `[SIM] #${seq} ${name.padEnd(14)} vRMSy=${payload.vRMSy.toFixed(2)} ` +
    `aRMSz=${payload.aRMSz.toFixed(3)} temp=${payload.temperature.toFixed(1)}°C ` +
    `acoustic=${payload.aucausticRMS.toFixed(1)}`
  );
  seq++;
}

function shutdown() {
  console.log("\n[SIM] Stopping...");
  clearInterval(timer);
  client.end(false, () => process.exit(0));
}
process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
