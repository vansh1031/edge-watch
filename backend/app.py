"""
app.py — Edge AI Predictive Maintenance Backend
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
New in this version:
  ✅ health_score in /api/prediction response
  ✅ MQTT auto-shutdown signal (topic: wired/control)
  ✅ Shutdown cooldown (no spam)
  ✅ Prediction + alert logging (predictions.log)
  ✅ Threshold crossing detection (only on edge, not every cycle)
  ✅ /api/alerts endpoint
  ✅ /api/model-status endpoint
  ✅ Built-in synthetic sensor (auto-starts when no real data arrives)
"""

from flask import Flask, jsonify, request
from flask_cors import CORS
import paho.mqtt.client as mqtt
import json
import csv
import os
import time
import logging
import threading
from collections import deque

from synthetic import SyntheticSensor

# ✅ Real ML engine
from predict import predict, predict_anomaly, MODELS_LOADED, check_thresholds

app = Flask(__name__)
CORS(app, origins="*", supports_credentials=True)

# ─── State ────────────────────────────────────────────────────
latest_data   = {}
latest_pred   = {}
recent_alerts = []          # in-memory alert log (last 50)
recent_rows   = deque(maxlen=2000)   # all readings since startup (real + synthetic)
_lock         = threading.Lock()
_last_real_ts = 0.0         # time.time() of last real MQTT reading

# ─── Synthetic Sensor CONFIG ──────────────────────────────────
# EDGEWATCH_SIM = auto   → synthetic data only while no real sensor data arrives (default)
#                 always → always synthetic, real MQTT data ignored
#                 off    → never synthetic
SIM_MODE         = os.environ.get("EDGEWATCH_SIM", "auto").lower()
SIM_SCENARIO     = os.environ.get("EDGEWATCH_SIM_SCENARIO", "demo").lower()   # demo | normal
SIM_INTERVAL     = float(os.environ.get("EDGEWATCH_SIM_INTERVAL", 2))         # seconds
SIM_IDLE_TIMEOUT = float(os.environ.get("EDGEWATCH_SIM_IDLE", 15))            # seconds without real data
SIM_BACKFILL     = int(os.environ.get("EDGEWATCH_SIM_BACKFILL", 1800))        # seconds of history at start
sim_state        = {"active": False, "phase": None}

# ─── MQTT CONFIG ──────────────────────────────────────────────
BROKER            = "broker.emqx.io"
PORT              = 1883
TOPIC_SENSOR      = "wired/rms/#"         # incoming sensor data
TOPIC_CONTROL     = "wired/control"       # outgoing: 1=STOP, 0=RUN
USERNAME          = "test"
PASSWORD          = "test"

# Auto-shutdown config
SHUTDOWN_COOLDOWN = 5 * 60     # 5 minutes between shutdown signals
SHUTDOWN_ENABLED  = True       # set False to disable auto-shutdown
_last_shutdown_ts = 0          # timestamp of last shutdown signal
_prev_status      = "normal"   # for edge detection (only on status CHANGE)

# ─── Logging Setup ────────────────────────────────────────────
logging.basicConfig(
    filename  = "predictions.log",
    level     = logging.INFO,
    format    = "%(asctime)s | %(levelname)s | %(message)s",
    datefmt   = "%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def log_prediction(pred: dict, data: dict):
    """Log every prediction to predictions.log"""
    log.info(
        f"PREDICTION | "
        f"fault={pred.get('fault_type','none')} | "
        f"health={pred.get('health_score','?')} | "
        f"rul={pred.get('rul_hours','?')}h | "
        f"status={pred.get('status','?')} | "
        f"confidence={pred.get('confidence','?')} | "
        f"anomaly={pred.get('anomaly','?')} | "
        f"motor_off={pred.get('motor_off','?')} | "
        f"trend={pred.get('trend','?')} | "
        f"temp={data.get('temperature','?')} | "
        f"vRMSy={data.get('vRMSy','?')}"
    )


def log_alert(alert: dict):
    """Log alert to predictions.log"""
    log.warning(
        f"ALERT | "
        f"type={alert.get('type')} | "
        f"severity={alert.get('severity')} | "
        f"message={alert.get('message')}"
    )


def log_mqtt_command(topic: str, payload: str, reason: str):
    """Log MQTT control command"""
    log.warning(
        f"MQTT_CMD | topic={topic} | payload={payload} | reason={reason}"
    )


# ─── CSV Config ───────────────────────────────────────────────
CSV_FILE    = "sensor_data.csv"
CSV_COLUMNS = [
    "timestamp", "seq", "key",
    "aRMSx", "aRMSy", "aRMSz",
    "vRMSx", "vRMSy", "vRMSz",
    "temperature", "aucausticRMS", "extra", "mac",
]

# ─── Recommendations ──────────────────────────────────────────
RECOMMENDATIONS = [
    {"id": "r1", "message": "Schedule bearing inspection within 48 hours",      "priority": "high",   "category": "maintenance"},
    {"id": "r2", "message": "Vibration levels trending upward — monitor closely","priority": "medium", "category": "risk"},
    {"id": "r3", "message": "Consider reducing operational speed by 10%",        "priority": "medium", "category": "optimization"},
    {"id": "r4", "message": "Lubrication maintenance overdue",                  "priority": "high",   "category": "maintenance"},
]

RECOMMENDATIONS_NORMAL = [
    {"id": "n1", "message": "All parameters within ISO 10816 limits — continue routine monitoring", "priority": "low",    "category": "optimization"},
    {"id": "n2", "message": "Next scheduled lubrication due within 14 days",                     "priority": "low",    "category": "maintenance"},
    {"id": "n3", "message": "Review vibration baseline at next planned shutdown",                "priority": "medium", "category": "maintenance"},
]


# ─── CSV Migration ─────────────────────────────────────────────
def migrate_old_csv():
    if not os.path.isfile(CSV_FILE):
        return
    with open(CSV_FILE, "r") as f:
        first_line = f.readline().strip()
    if first_line.startswith("timestamp"):
        print("[CSV] Already migrated.")
        return

    print("[CSV] Migrating old CSV...")
    backup = CSV_FILE.replace(".csv", "_backup.csv")
    with open(CSV_FILE, "r") as src, open(backup, "w") as dst:
        dst.write(src.read())

    old_rows = []
    with open(CSV_FILE, "r") as f:
        for row in csv.reader(f):
            if len(row) >= 12:
                old_rows.append(row)

    now_ms = int(time.time() * 1000)
    total  = len(old_rows)
    with open(CSV_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for i, row in enumerate(old_rows):
            writer.writerow({
                "timestamp":    now_ms - ((total - i) * 6000),
                "seq":          row[1].strip(),
                "key":          row[0].strip(),
                "aRMSx":        row[2].strip(), "aRMSy": row[3].strip(), "aRMSz": row[4].strip(),
                "vRMSx":        row[5].strip(), "vRMSy": row[6].strip(), "vRMSz": row[7].strip(),
                "temperature":  row[8].strip(),
                "aucausticRMS": row[9].strip(),
                "extra":        row[10].strip(),
                "mac":          row[11].strip(),
            })
    print(f"[CSV] Migration done — {total} rows.")


# ─── Helpers ──────────────────────────────────────────────────
def safe_float(val, default=0.0):
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def parse_csv_rows(rows):
    result = []
    for row in rows:
        converted = {}
        for k, v in row.items():
            v = v.strip() if isinstance(v, str) else v
            try:
                converted[k] = int(v)
            except (ValueError, TypeError):
                try:
                    converted[k] = float(v)
                except (ValueError, TypeError):
                    converted[k] = v
        result.append(converted)
    return result


# ─── MQTT Auto-Shutdown ────────────────────────────────────────
def maybe_send_shutdown(pred: dict):
    """
    Send STOP signal (payload=1) to TOPIC_CONTROL if:
      - Status is CRITICAL
      - Cooldown has passed (5 min)
      - Status just changed TO critical (edge detection, not repeated)
      - Motor is not already OFF
    Send RUN signal (payload=0) when status recovers to normal.
    """
    global _last_shutdown_ts, _prev_status

    if not SHUTDOWN_ENABLED:
        return

    # Never send real STOP/RUN commands because of simulator data
    if latest_data.get("simulated"):
        return

    current_status = pred.get("status", "normal")
    motor_off      = pred.get("motor_off", False)
    now            = time.time()
    cooldown_ok    = (now - _last_shutdown_ts) >= SHUTDOWN_COOLDOWN

    # ── STOP: Critical threshold crossing ─────────────────────
    if (current_status == "critical"
            and _prev_status != "critical"
            and not motor_off
            and cooldown_ok):

        reason = f"fault={pred.get('fault_type')} health={pred.get('health_score')}"
        try:
            mqtt_client.publish(TOPIC_CONTROL, payload="1", qos=1)
            _last_shutdown_ts = now
            log_mqtt_command(TOPIC_CONTROL, "1", f"AUTO-STOP: {reason}")
            print(f"[MQTT] 🔴 STOP signal sent — {reason}")
        except Exception as e:
            print(f"[MQTT] Shutdown publish error: {e}")

    # ── RUN: Recovery from critical to normal ─────────────────
    elif (current_status == "normal"
            and _prev_status == "critical"
            and not motor_off):
        try:
            mqtt_client.publish(TOPIC_CONTROL, payload="0", qos=1)
            log_mqtt_command(TOPIC_CONTROL, "0", "AUTO-RUN: status recovered to normal")
            print(f"[MQTT] 🟢 RUN signal sent — status recovered")
        except Exception as e:
            print(f"[MQTT] Run publish error: {e}")

    _prev_status = current_status


# ─── Alert Memory ─────────────────────────────────────────────
def store_alerts(alerts: list):
    """Keep last 50 alerts in memory for /api/alerts endpoint."""
    global recent_alerts
    now_ms = int(time.time() * 1000)
    for a in alerts:
        if "timestamp" not in a:
            a["timestamp"] = now_ms
        # Skip repeats of the same alert within 60 s (sensor reports every few seconds)
        if any(r["type"] == a.get("type") and r["severity"] == a.get("severity")
               and a["timestamp"] - r["timestamp"] < 60_000 for r in recent_alerts):
            continue
        a["id"] = f"{a.get('type')}-{a['timestamp']}"
        recent_alerts.append(a)
        log_alert(a)
    # Keep only last 50
    recent_alerts = recent_alerts[-50:]


# ─── MQTT Callbacks ───────────────────────────────────────────
def on_connect(client, userdata, flags, rc):
    print("Connected to MQTT Broker:", rc)
    client.subscribe(TOPIC_SENSOR)
    print(f"[MQTT] Subscribed: {TOPIC_SENSOR}")


def on_message(client, userdata, msg):
    global _last_real_ts
    try:
        payload = json.loads(msg.payload.decode())
        # Built-in synthetic sensor already running → ignore the old external simulator
        if SIM_MODE == "always" or (payload.get("simulated") and sim_state["active"]):
            return
        payload["seq"]       = payload.get("timestamp")
        payload["timestamp"] = int(time.time() * 1000)
        if not payload.get("simulated"):
            _last_real_ts = time.time()
        process_reading(payload, persist=not payload.get("simulated"))
    except Exception as e:
        print(f"[MQTT] Error: {e}")
        log.error(f"MQTT message error: {e}")


def process_reading(payload: dict, persist: bool = True, verbose: bool = True):
    """Run one reading through ML → alerts → shutdown check → storage."""
    global latest_data, latest_pred
    with _lock:
        latest_data = payload

        # ✅ Real ML prediction (with temporal context from SensorBuffer)
        latest_pred = predict(latest_data)

        if verbose:
            print(
                f"[{'SIM' if payload.get('simulated') else 'MQTT'}] fault={latest_pred['fault_type']} | "
                f"health={latest_pred['health_score']} | "
                f"rul={latest_pred['rul_hours']}h | "
                f"status={latest_pred['status']} | "
                f"motor_off={latest_pred['motor_off']} | "
                f"trend={latest_pred['trend']}"
            )

        # Store alerts if any
        if latest_pred.get("alerts"):
            store_alerts(latest_pred["alerts"])

        # Auto-shutdown check
        maybe_send_shutdown(latest_pred)

        # Log prediction (real data only — synthetic would flood the log)
        if persist:
            log_prediction(latest_pred, latest_data)

        recent_rows.append({**latest_data, "health_score": latest_pred.get("health_score")})

        # Synthetic readings are kept in memory only, so sensor_data.csv stays real data
        if persist:
            save_to_csv(latest_data)


# ─── Synthetic Sensor Loop ────────────────────────────────────
def sim_loop():
    sensor = SyntheticSensor(interval_s=SIM_INTERVAL, scenario=SIM_SCENARIO)
    backfilled = False
    print(f"[SIM] Synthetic sensor mode={SIM_MODE} scenario={SIM_SCENARIO} interval={SIM_INTERVAL}s")
    while True:
        try:
            idle = (time.time() - _last_real_ts) >= SIM_IDLE_TIMEOUT
            should_run = SIM_MODE == "always" or (SIM_MODE == "auto" and idle)

            if should_run and not sim_state["active"]:
                print("[SIM] ▶ No real sensor data — synthetic sensor started")
                sim_state["active"] = True
                if not backfilled:
                    rows = sensor.backfill(SIM_BACKFILL)
                    # Only the last few go through the model (warms up EMA/trend buffer)
                    warm = rows[-30:]
                    with _lock:
                        recent_rows.extend(rows[:-len(warm)])
                    for r in warm:
                        process_reading(r, persist=False, verbose=False)
                    backfilled = True
            elif not should_run and sim_state["active"]:
                print("[SIM] ⏸ Real sensor data detected — synthetic sensor paused")
                sim_state["active"] = False
                sim_state["phase"]  = None

            if sim_state["active"]:
                reading = sensor.next()
                sim_state["phase"] = reading.pop("scenario")
                process_reading(reading, persist=False, verbose=sensor.step % 15 == 0)
        except Exception as e:
            print(f"[SIM] Error: {e}")
            log.error(f"SIM error: {e}")
        time.sleep(SIM_INTERVAL)


def save_to_csv(data):
    file_exists = os.path.isfile(CSV_FILE)
    has_header  = False
    if file_exists:
        with open(CSV_FILE, "r") as f:
            has_header = f.readline().strip().startswith("timestamp")
    with open(CSV_FILE, mode="a", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        if not has_header:
            writer.writeheader()
        writer.writerow(data)


# ─── Routes ───────────────────────────────────────────────────

@app.route("/api/live-data")
def get_live_data():
    if not latest_data:
        return jsonify({"error": "No data yet"}), 503
    return jsonify(latest_data)


@app.route("/api/history")
def get_history():
    try:
        range_param = request.args.get("range", "all")
        limit       = int(request.args.get("limit", 1000))
        try:
            with open(CSV_FILE, "r") as file:
                reader = list(csv.DictReader(file))
        except FileNotFoundError:
            reader = []
        result = parse_csv_rows(reader)
        # Add in-memory synthetic readings (real ones are already in the CSV)
        result += [dict(r) for r in list(recent_rows) if r.get("simulated")]
        # Some old CSV rows have a blank/garbled timestamp — skip them
        result = [r for r in result if isinstance(r.get("timestamp"), (int, float))]
        result.sort(key=lambda r: r["timestamp"])
        if range_param != "all":
            now_ms    = int(time.time() * 1000)
            range_map = {"1m": 60_000, "5m": 300_000, "1h": 3_600_000}
            cutoff    = now_ms - range_map.get(range_param, 300_000)
            result    = [r for r in result if r.get("timestamp", 0) >= cutoff]
        return jsonify(result[-limit:])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/prediction")
def get_prediction():
    """
    ✅ Returns full prediction including health_score.
    API Contract:
      { failure_risk, rul_hours, status, fault_type,
        confidence, health_score, motor_off, trend }
    """
    if not latest_data:
        return jsonify({
            "failure_risk": 0,
            "rul_hours":    720,
            "status":       "normal",
            "fault_type":   "none",
            "confidence":   0.5,
            "health_score": 95.0,
            "motor_off":    False,
            "trend":        0.0,
        })

    result = latest_pred if latest_pred else predict(latest_data)

    return jsonify({
        "failure_risk": result["failure_risk"],
        "rul_hours":    result["rul_hours"],
        "status":       result["status"],
        "fault_type":   result["fault_type"],
        "confidence":   result["confidence"],
        "health_score": result["health_score"],   # ✅ NOW INCLUDED
        "motor_off":    result.get("motor_off", False),
        "trend":        result.get("trend", 0.0),
    })


@app.route("/api/anomaly")
def get_anomaly():
    """Uses Isolation Forest output."""
    if not latest_data:
        return jsonify({"anomaly": False, "status": "normal", "healthScore": 95, "alerts": []})
    if latest_pred:
        return jsonify({
            "anomaly":     latest_pred["anomaly"],
            "status":      latest_pred["status"],
            "healthScore": latest_pred["health_score"],
            "alerts":      latest_pred.get("alerts", []),
        })
    return jsonify(predict_anomaly(latest_data))


@app.route("/api/alerts")
def get_alerts():
    """
    ✅ New endpoint — returns recent threshold alerts.
    Query: ?limit=N (default 20)
    """
    limit = int(request.args.get("limit", 20))
    return jsonify(recent_alerts[-limit:])


@app.route("/api/forecast")
def get_forecast():
    snapshot = list(recent_rows)
    if len(snapshot) >= 2:
        rows = snapshot[-30:]
    else:
        try:
            with open(CSV_FILE, "r") as file:
                rows = list(csv.DictReader(file))[-30:]
        except FileNotFoundError:
            rows = []

    def make_series(rows, key):
        actual = [
            {"timestamp": safe_float(r.get("timestamp")), "value": safe_float(r.get(key)), "predicted": False}
            for r in rows
        ]
        predicted = []
        if len(actual) >= 2:
            last_val  = actual[-1]["value"]
            last_ts   = actual[-1]["timestamp"]
            avg_delta = (actual[-1]["value"] - actual[0]["value"]) / max(len(actual) - 1, 1)
            step_ms   = max((last_ts - actual[0]["timestamp"]) / max(len(actual) - 1, 1), 1000)
            for i in range(1, 6):
                predicted.append({
                    "timestamp": last_ts + (i * step_ms),
                    "value":     round(last_val + avg_delta * i, 4),
                    "predicted": True,
                })
        return actual + predicted

    # Health score series — per-reading EMA health from memory
    health_series = [
        {"timestamp": safe_float(r.get("timestamp")), "value": r["health_score"], "predicted": False}
        for r in snapshot[-60:] if r.get("health_score") is not None
    ]

    return jsonify({
        "vRMSy":       make_series(rows, "vRMSy"),
        "temperature": make_series(rows, "temperature"),
        "healthScore": health_series,
    })


@app.route("/api/recommendations")
def get_recommendations():
    """Dynamic recommendations based on ML status + fault type."""
    fault  = latest_pred.get("fault_type", "none")  if latest_pred else "none"
    conf   = latest_pred.get("confidence", 0)       if latest_pred else 0
    status = latest_pred.get("status", "normal")    if latest_pred else "normal"

    if latest_pred.get("motor_off"):
        recs = [RECOMMENDATIONS_NORMAL[0], {"id": "off1", "message": "Motor stopped — good window for inspection and lubrication", "priority": "low", "category": "maintenance"}]
    elif status == "normal":
        recs = list(RECOMMENDATIONS_NORMAL)
    elif status == "warning":
        recs = [RECOMMENDATIONS[1], RECOMMENDATIONS[2], RECOMMENDATIONS[0]]
    else:
        recs = list(RECOMMENDATIONS)

    ml_rec = None
    if fault == "bearing fault":
        ml_rec = {"id": "ml1", "message": f"ML: Bearing fault detected ({conf*100:.0f}% confidence) — inspect immediately", "priority": "high",   "category": "maintenance"}
    elif fault == "misalignment":
        ml_rec = {"id": "ml1", "message": f"ML: Shaft misalignment ({conf*100:.0f}% confidence) — check coupling",          "priority": "high",   "category": "maintenance"}
    elif fault == "imbalance":
        ml_rec = {"id": "ml1", "message": f"ML: Rotor imbalance ({conf*100:.0f}% confidence) — schedule balancing",          "priority": "medium", "category": "maintenance"}
    elif fault == "overheating":
        ml_rec = {"id": "ml1", "message": f"ML: Overheating ({conf*100:.0f}% confidence) — check cooling system",            "priority": "high",   "category": "risk"}

    if ml_rec:
        recs.insert(0, ml_rec)

    return jsonify(recs)


@app.route("/api/model-status")
def model_status():
    return jsonify({
        "models_loaded":   MODELS_LOADED,
        "mode":            "ML (RF + IsoForest + SensorBuffer)" if MODELS_LOADED else "Threshold Fallback",
        "last_fault":      latest_pred.get("fault_type")    if latest_pred else None,
        "last_health":     latest_pred.get("health_score")  if latest_pred else None,
        "last_confidence": latest_pred.get("confidence")    if latest_pred else None,
        "last_rul":        latest_pred.get("rul_hours")     if latest_pred else None,
        "motor_off":       latest_pred.get("motor_off")     if latest_pred else None,
        "trend":           latest_pred.get("trend")         if latest_pred else None,
        "shutdown_enabled":SHUTDOWN_ENABLED,
        "control_topic":   TOPIC_CONTROL,
    })


@app.route("/api/health-check")
def health_check():
    return jsonify({
        "status":        "ok",
        "has_data":      bool(latest_data),
        "models_loaded": MODELS_LOADED,
        "last_ts":       latest_data.get("timestamp"),
        "simulation":    sim_state,
    })


@app.route("/api/simulation")
def simulation_status():
    return jsonify({
        "mode":         SIM_MODE,
        "scenario":     SIM_SCENARIO,
        "active":       sim_state["active"],
        "phase":        sim_state["phase"],
        "interval_s":   SIM_INTERVAL,
        "idle_timeout": SIM_IDLE_TIMEOUT,
    })


# ─── MQTT Client ──────────────────────────────────────────────
mqtt_client = mqtt.Client()
mqtt_client.username_pw_set(USERNAME, PASSWORD)
mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message
# connect_async + loop_start keeps retrying in the background, so the
# API still starts when the broker/DNS isn't reachable yet at boot.
mqtt_client.connect_async(BROKER, PORT, 60)
mqtt_client.loop_start()

migrate_old_csv()

if SIM_MODE in ("auto", "always"):
    threading.Thread(target=sim_loop, daemon=True, name="synthetic-sensor").start()

print(f"[APP] ML Mode : {'✅ REAL MODEL' if MODELS_LOADED else '⚠️  FALLBACK RULES'}")
print(f"[APP] Shutdown: {'✅ ENABLED' if SHUTDOWN_ENABLED else '❌ DISABLED'} → topic={TOPIC_CONTROL}")
print(f"[APP] Log file: predictions.log")

# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5005, debug=False, threaded=True)