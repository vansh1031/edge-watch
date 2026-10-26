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
  ✅ STOP always sent on critical (control.py), no automatic RUN by default
  ✅ MQTT / control settings from environment or backend/.env
  ✅ Sensor-offline alert — a dead sensor is never hidden behind synthetic data
  ✅ predictions.log rotates (10 MB × 5)
  ✅ SQLite storage (db.py): readings, alerts with acknowledgement, STOP/RUN audit
  ✅ Live stream: GET /api/stream (Server-Sent Events)
"""

from flask import Flask, Response, jsonify, request, stream_with_context
from flask_cors import CORS
import paho.mqtt.client as mqtt
import json
import csv
import os
import time
import logging
import queue
from logging.handlers import RotatingFileHandler
import threading
from collections import deque

from synthetic import SyntheticSensor
from control import ShutdownController
from db import Store


# ─── .env loader ──────────────────────────────────────────────
# KEY=VALUE lines from backend/.env; real environment variables win.
def _load_env_file(path=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")):
    if not os.path.isfile(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_env_file()


def _env_bool(name, default):
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")

# ✅ Real ML engine
from predict import predict, predict_anomaly, MODELS_LOADED, check_thresholds, THRESHOLDS

app = Flask(__name__)
CORS(app, origins="*", supports_credentials=True)

# ─── State ────────────────────────────────────────────────────
latest_data   = {}
latest_pred   = {}
recent_rows   = deque(maxlen=2000)   # all readings since startup (real + synthetic)
_lock         = threading.Lock()
_last_real_ts = 0.0         # time.time() of last real MQTT reading

# ─── Synthetic Sensor CONFIG ──────────────────────────────────
# EDGEWATCH_SIM = auto     → synthetic data only until the first real reading arrives (default).
#                            After that, a silent sensor raises a "sensor offline" alert
#                            instead of being replaced by fake data.
#                 fallback → synthetic data whenever real data stops for SIM_IDLE seconds
#                            (demo setups only — hides a dead sensor)
#                 always   → always synthetic, real MQTT data ignored
#                 off      → never synthetic
SIM_MODE         = os.environ.get("EDGEWATCH_SIM", "auto").lower()
SIM_SCENARIO     = os.environ.get("EDGEWATCH_SIM_SCENARIO", "demo").lower()   # demo | normal
SIM_INTERVAL     = float(os.environ.get("EDGEWATCH_SIM_INTERVAL", 2))         # seconds
SIM_IDLE_TIMEOUT = float(os.environ.get("EDGEWATCH_SIM_IDLE", 15))            # seconds without real data
SIM_BACKFILL     = int(os.environ.get("EDGEWATCH_SIM_BACKFILL", 1800))        # seconds of history at start
sim_state        = {"active": False, "phase": None}

# Sensor counts as offline after this many seconds without a real reading
SENSOR_TIMEOUT   = float(os.environ.get("EDGEWATCH_SENSOR_TIMEOUT", 30))

# ─── Storage ──────────────────────────────────────────────────
BACKEND_DIR      = os.path.dirname(os.path.abspath(__file__))
DB_PATH          = os.environ.get("EDGEWATCH_DB", os.path.join(BACKEND_DIR, "edgewatch.db"))
RETENTION_DAYS   = float(os.environ.get("EDGEWATCH_RETENTION_DAYS", 365))   # 0 = keep forever
store            = Store(DB_PATH)

# ─── MQTT CONFIG ──────────────────────────────────────────────
# Defaults match the original setup. The public broker is NOT safe for a real
# machine: anyone can publish fake readings or STOP/RUN commands.
# Set your own broker in backend/.env (see .env.example).
BROKER            = os.environ.get("MQTT_BROKER", "broker.emqx.io")
MQTT_TLS          = _env_bool("MQTT_TLS", False)
PORT              = int(os.environ.get("MQTT_PORT", 8883 if MQTT_TLS else 1883))
MQTT_CA_CERT      = os.environ.get("MQTT_CA_CERT") or None   # None = system CAs
TOPIC_SENSOR      = os.environ.get("MQTT_TOPIC_SENSOR",  "wired/rms/#")    # incoming sensor data
TOPIC_CONTROL     = os.environ.get("MQTT_TOPIC_CONTROL", "wired/control")  # outgoing: 1=STOP, 0=RUN
USERNAME          = os.environ.get("MQTT_USERNAME", "test")
PASSWORD          = os.environ.get("MQTT_PASSWORD", "test")
PUBLIC_BROKERS    = {"broker.emqx.io", "test.mosquitto.org", "broker.hivemq.com", "mqtt.eclipseprojects.io"}

# Auto-shutdown config
SHUTDOWN_ENABLED  = _env_bool("SHUTDOWN_ENABLED", True)
SHUTDOWN_RESEND   = float(os.environ.get("SHUTDOWN_RESEND_S", 60))    # re-send STOP while still critical
AUTO_RESUME       = _env_bool("AUTO_RESUME", False)                   # automatic RUN after recovery
RESUME_HOLD       = float(os.environ.get("RESUME_HOLD_S", 120))       # normal this long before auto RUN
CONTROL_TOKEN     = os.environ.get("CONTROL_TOKEN", "")               # required for POST /api/control/resume

# ─── Logging Setup ────────────────────────────────────────────
_log_handler = RotatingFileHandler("predictions.log", maxBytes=10 * 1024 * 1024, backupCount=5)
_log_handler.setFormatter(logging.Formatter(
    "%(asctime)s | %(levelname)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
log = logging.getLogger("edgewatch")
log.setLevel(logging.INFO)
log.addHandler(_log_handler)


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
# sensor_data.csv is no longer written. It is imported into SQLite once at
# startup and kept as a backup. Export: GET /api/history/export.csv
CSV_FILE    = os.path.join(BACKEND_DIR, "sensor_data.csv")
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


# ─── Live Event Stream (Server-Sent Events) ───────────────────
class Broadcaster:
    """Fan-out of events to every open /api/stream connection."""
    def __init__(self):
        self._subs = set()
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q = queue.Queue(maxsize=200)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            self._subs.discard(q)

    def publish(self, event: str, data):
        msg = f"event: {event}\ndata: {json.dumps(data)}\n\n"
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(msg)
            except queue.Full:
                pass   # slow client — it will catch up from the next heartbeat


events = Broadcaster()


# ─── MQTT Auto-Shutdown ────────────────────────────────────────
def publish_control(payload: str, reason: str) -> bool:
    """Publish STOP ("1") / RUN ("0"). Returns False if it could not be sent."""
    try:
        info = mqtt_client.publish(TOPIC_CONTROL, payload=payload, qos=1)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            print(f"[MQTT] ⚠️  Control publish failed (rc={info.rc}) — will retry")
            return False
    except Exception as e:
        print(f"[MQTT] Control publish error: {e}")
        return False
    log_mqtt_command(TOPIC_CONTROL, payload, reason)
    store.add_command(payload, reason, TOPIC_CONTROL)
    print(f"[MQTT] {'🔴 STOP' if payload == '1' else '🟢 RUN'} signal sent — {reason}")
    return True


controller = ShutdownController(
    publish       = publish_control,
    enabled       = SHUTDOWN_ENABLED,
    resend_s      = SHUTDOWN_RESEND,
    auto_resume   = AUTO_RESUME,
    resume_hold_s = RESUME_HOLD,
)


def maybe_send_shutdown(pred: dict):
    # Never send real STOP/RUN commands because of simulator data
    controller.update(pred, simulated=bool(latest_data.get("simulated")))


# ─── Alerts ───────────────────────────────────────────────────
def store_alerts(alerts: list, simulated: bool = False):
    """Save alerts to SQLite (repeats within 60 s are skipped) and push them live."""
    for a in alerts:
        a.setdefault("machine", latest_data.get("mac"))
        stored = store.add_alert(a, simulated=simulated)
        if stored:
            log_alert(stored)
            events.publish("alert", stored)


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
            store_alerts(latest_pred["alerts"], simulated=bool(payload.get("simulated")))

        # Auto-shutdown check
        maybe_send_shutdown(latest_pred)

        # Log prediction (real data only — synthetic would flood the log)
        if persist:
            log_prediction(latest_pred, latest_data)

        recent_rows.append({**latest_data, "health_score": latest_pred.get("health_score")})

        # Synthetic readings are kept in memory only, so the database stays real data
        if persist:
            store.add_reading(latest_data)

        events.publish("reading", live_payload())
        events.publish("prediction", prediction_payload())


# ─── Synthetic Sensor Loop ────────────────────────────────────
def sim_loop():
    sensor = SyntheticSensor(interval_s=SIM_INTERVAL, scenario=SIM_SCENARIO)
    backfilled = False
    print(f"[SIM] Synthetic sensor mode={SIM_MODE} scenario={SIM_SCENARIO} interval={SIM_INTERVAL}s")
    while True:
        try:
            idle = (time.time() - _last_real_ts) >= SIM_IDLE_TIMEOUT
            should_run = (SIM_MODE == "always"
                          or (SIM_MODE == "auto" and _last_real_ts == 0)
                          or (SIM_MODE == "fallback" and idle))

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


# ─── Sensor Watchdog ──────────────────────────────────────────
def sensor_age_s():
    """Seconds since the last real reading, or None if none arrived yet."""
    return round(time.time() - _last_real_ts, 1) if _last_real_ts else None


def data_freshness() -> dict:
    """Where the current reading came from and how old it is."""
    if latest_data.get("simulated"):
        return {"simulated": True, "stale": False, "age_s": None}
    age = sensor_age_s()
    return {"simulated": False, "stale": age is not None and age >= SENSOR_TIMEOUT, "age_s": age}


def sensor_levels(data: dict) -> dict:
    """normal / warning / critical per field, using the same THRESHOLDS as the alerts."""
    levels = {}
    for field, (warn_val, crit_val) in THRESHOLDS.items():
        val = safe_float(data.get(field))
        levels[field] = "critical" if val >= crit_val else "warning" if val >= warn_val else "normal"
    return levels


def watchdog_loop():
    """Raise an alert when a sensor that was sending data goes silent."""
    offline    = False
    next_prune = 0.0
    while True:
        try:
            if RETENTION_DAYS > 0 and time.time() >= next_prune:
                next_prune = time.time() + 3600
                removed = store.prune(RETENTION_DAYS)
                if removed:
                    print(f"[DB] Pruned {removed} readings older than {RETENTION_DAYS:g} days")
            age = sensor_age_s()
            if age is not None and age >= SENSOR_TIMEOUT and not offline:
                offline = True
                print(f"[WATCHDOG] 🔴 No sensor data for {age:.0f}s")
                with _lock:
                    store_alerts([{
                        "type":     "sensor_offline",
                        "message":  f"No data from sensor for {age:.0f} s — check sensor power, wiring and MQTT link",
                        "severity": "critical",
                    }])
            elif offline and age is not None and age < SENSOR_TIMEOUT:
                offline = False
                print("[WATCHDOG] 🟢 Sensor back online")
                with _lock:
                    store_alerts([{
                        "type":     "sensor_online",
                        "message":  "Sensor data is arriving again",
                        "severity": "normal",
                    }])
        except Exception as e:
            print(f"[WATCHDOG] Error: {e}")
        time.sleep(2)


# ─── Routes ───────────────────────────────────────────────────

def live_payload() -> dict:
    return {**latest_data, **data_freshness(), "levels": sensor_levels(latest_data)}


def prediction_payload() -> dict:
    result = latest_pred
    return {
        "failure_risk": result["failure_risk"],
        "rul_hours":    result["rul_hours"],
        "status":       result["status"],
        "fault_type":   result["fault_type"],
        "confidence":   result["confidence"],
        "health_score": result["health_score"],
        "motor_off":    result.get("motor_off", False),
        "trend":        result.get("trend", 0.0),
        **data_freshness(),
    }


def control_payload() -> dict:
    return {
        **controller.state(),
        "manual_resume":   bool(CONTROL_TOKEN),
        "mqtt_connected":  mqtt_client.is_connected(),
        "machine_status":  latest_pred.get("status") if latest_pred else None,
        "motor_off":       latest_pred.get("motor_off") if latest_pred else None,
        "control_topic":   TOPIC_CONTROL,
    }


def status_payload() -> dict:
    """Heartbeat for the stream: freshness changes even when no reading arrives."""
    return {"has_data": bool(latest_data), **(data_freshness() if latest_data else {}),
            "control": control_payload()}


@app.route("/api/live-data")
def get_live_data():
    if not latest_data:
        return jsonify({"error": "No data yet"}), 503
    return jsonify(live_payload())


@app.route("/api/stream")
def stream():
    """
    Server-Sent Events. Event types:
      reading    → same body as /api/live-data
      prediction → same body as /api/prediction
      alert      → a newly stored alert
      status     → every 2 s: has_data, stale/age_s, control state
    """
    q = events.subscribe()

    def gen():
        try:
            yield "retry: 3000\n\n"
            yield f"event: status\ndata: {json.dumps(status_payload())}\n\n"
            if latest_data:
                yield f"event: reading\ndata: {json.dumps(live_payload())}\n\n"
                yield f"event: prediction\ndata: {json.dumps(prediction_payload())}\n\n"
            # Heartbeat every 2 s even while readings keep arriving — it carries
            # the control state (STOP held) and the sensor age
            next_status = time.time() + 2
            while True:
                try:
                    yield q.get(timeout=max(0.0, next_status - time.time()))
                except queue.Empty:
                    pass
                if time.time() >= next_status:
                    next_status = time.time() + 2
                    yield f"event: status\ndata: {json.dumps(status_payload())}\n\n"
        finally:
            events.unsubscribe(q)

    return Response(stream_with_context(gen()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/history")
def get_history():
    """
    Chart data. ?range=1m|5m|1h|all &limit=N
    Long ranges are thinned evenly to `limit` points (newest point always included).
    """
    try:
        range_param = request.args.get("range", "all")
        limit       = int(request.args.get("limit", 1000))
        range_map   = {"1m": 60_000, "5m": 300_000, "1h": 3_600_000}
        now_ms      = int(time.time() * 1000)
        since       = now_ms - range_map.get(range_param, 300_000) if range_param != "all" else None
        result      = store.history_range(since, limit)
        # Demo mode: synthetic readings live in memory only
        sim = [dict(r) for r in list(recent_rows) if r.get("simulated") and (since is None or r["timestamp"] >= since)]
        if sim:
            result = sorted(result + sim, key=lambda r: r["timestamp"])[-limit:]
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _int_arg(name, default=None):
    val = request.args.get(name)
    try:
        return int(val) if val not in (None, "") else default
    except ValueError:
        return default


@app.route("/api/history/search")
def history_search():
    """
    History page. ?start_ms=&end_ms=&q=&page=1&limit=20
    Returns { data: [...newest first], total, page, limit }. Real readings only.
    """
    rows, total = store.history_page(
        start_ms = _int_arg("start_ms"),
        end_ms   = _int_arg("end_ms"),
        q        = request.args.get("q", "").strip(),
        page     = max(1, _int_arg("page", 1)),
        limit    = min(max(1, _int_arg("limit", 20)), 500),
    )
    return jsonify({"data": rows, "total": total, "page": max(1, _int_arg("page", 1)),
                    "limit": min(max(1, _int_arg("limit", 20)), 500)})


@app.route("/api/history/export.csv")
def history_export():
    """Download readings as CSV (same columns as the old sensor_data.csv). ?start_ms=&end_ms="""
    rows, _ = store.history_page(start_ms=_int_arg("start_ms"), end_ms=_int_arg("end_ms"),
                                 page=1, limit=10**9)
    def gen():
        cols = ["timestamp", "seq", "aRMSx", "aRMSy", "aRMSz", "vRMSx", "vRMSy", "vRMSz",
                "temperature", "aucausticRMS", "extra", "mac"]
        yield ",".join(cols) + "\n"
        for r in reversed(rows):
            yield ",".join("" if r.get(c) is None else str(r.get(c)) for c in cols) + "\n"
    return Response(gen(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=edgewatch_readings.csv"})


@app.route("/api/prediction")
def get_prediction():
    """
    ✅ Returns full prediction including health_score.
    API Contract:
      { failure_risk, rul_hours, status, fault_type,
        confidence, health_score, motor_off, trend,
        simulated, stale, age_s }
    503 while no reading has arrived yet.
    """
    if not latest_data:
        # No reading yet — say so instead of inventing a healthy machine
        return jsonify({"error": "No data yet"}), 503

    return jsonify(prediction_payload())


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
    Recent alerts, newest first.
    Query: ?limit=N (default 20) &unacked=1 (only unacknowledged warnings/criticals)
    """
    limit = min(_int_arg("limit", 20), 500)
    return jsonify(store.alerts(limit, unacked_only=request.args.get("unacked") == "1"))


@app.route("/api/alerts/<int:alert_id>/ack", methods=["POST"])
def ack_alert(alert_id):
    """Acknowledge an alert. Body: {"operator": "name"}"""
    who = str((request.get_json(silent=True) or {}).get("operator", "")).strip()
    if not who:
        return jsonify({"error": "operator name required"}), 400
    acked = store.ack_alert(alert_id, who[:60])
    if not acked:
        return jsonify({"error": "Alert not found or already acknowledged"}), 404
    log.info(f"ALERT_ACK | id={alert_id} | by={who}")
    events.publish("alert_ack", acked)
    return jsonify(acked)


@app.route("/api/forecast")
def get_forecast():
    snapshot = list(recent_rows)
    if len(snapshot) >= 2:
        rows = snapshot[-30:]
    else:
        rows = store.latest_readings(30)

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
        "control":         controller.state(),
    })


@app.route("/api/control/status")
def control_status():
    """What the dashboard control panel shows."""
    return jsonify(control_payload())


@app.route("/api/control/history")
def control_history():
    """Last STOP / RUN commands (audit trail). ?limit=N"""
    return jsonify(store.commands(min(_int_arg("limit", 20), 500)))


@app.route("/api/thresholds")
def thresholds():
    """Warning / critical limits used for alerts and dashboard colours."""
    return jsonify({k: {"warning": w, "critical": c} for k, (w, c) in THRESHOLDS.items()})


@app.route("/api/control/resume", methods=["POST"])
def control_resume():
    """
    Operator sends RUN after a STOP.
    Needs header  X-Control-Token: <CONTROL_TOKEN>  — disabled if CONTROL_TOKEN is not set.
    Refused while the machine is still critical.
    """
    if not CONTROL_TOKEN:
        return jsonify({"error": "Manual resume disabled — set CONTROL_TOKEN in backend/.env"}), 403
    if request.headers.get("X-Control-Token") != CONTROL_TOKEN:
        return jsonify({"error": "Invalid control token"}), 401
    if latest_pred.get("status") == "critical" and not latest_pred.get("motor_off"):
        return jsonify({"error": "Machine is still critical — not resuming"}), 409
    who = (request.get_json(silent=True) or {}).get("operator", "unknown")
    if not controller.resume(f"MANUAL-RUN by {who}"):
        return jsonify({"error": "Could not publish RUN — MQTT not connected"}), 503
    return jsonify({"ok": True, "control": control_payload()})


@app.route("/api/health-check")
def health_check():
    return jsonify({
        "status":        "ok",
        "has_data":      bool(latest_data),
        "models_loaded": MODELS_LOADED,
        "last_ts":       latest_data.get("timestamp"),
        "simulation":    sim_state,
        "sensor": {
            "last_real_age_s": sensor_age_s(),
            "offline":         (sensor_age_s() or 0) >= SENSOR_TIMEOUT,
            "timeout_s":       SENSOR_TIMEOUT,
        },
        "mqtt": {
            "connected": mqtt_client.is_connected(),
            "broker":    BROKER,
            "tls":       MQTT_TLS,
        },
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
if MQTT_TLS:
    mqtt_client.tls_set(ca_certs=MQTT_CA_CERT)
mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message
# connect_async + loop_start keeps retrying in the background, so the
# API still starts when the broker/DNS isn't reachable yet at boot.
mqtt_client.connect_async(BROKER, PORT, 60)
mqtt_client.loop_start()

migrate_old_csv()
_imported, _skipped = store.import_csv(CSV_FILE)
if _imported or _skipped:
    print(f"[DB] Imported {_imported} readings from {os.path.basename(CSV_FILE)} ({_skipped} damaged/simulated rows skipped)")

if SIM_MODE in ("auto", "fallback", "always"):
    threading.Thread(target=sim_loop, daemon=True, name="synthetic-sensor").start()
threading.Thread(target=watchdog_loop, daemon=True, name="sensor-watchdog").start()

print(f"[APP] ML Mode : {'✅ REAL MODEL' if MODELS_LOADED else '⚠️  FALLBACK RULES'}")
print(f"[APP] MQTT    : {BROKER}:{PORT} {'(TLS)' if MQTT_TLS else '(no TLS)'}")
if BROKER in PUBLIC_BROKERS or not MQTT_TLS:
    print("[APP] ⚠️  INSECURE MQTT — public broker and/or no TLS. Anyone can send fake "
          "readings or STOP/RUN commands. Configure backend/.env before real use.")
print(f"[APP] Shutdown: {'✅ ENABLED' if SHUTDOWN_ENABLED else '❌ DISABLED'} → topic={TOPIC_CONTROL}"
      f" | auto-resume={'ON' if AUTO_RESUME else 'OFF'} | manual resume={'ON' if CONTROL_TOKEN else 'OFF'}")
print(f"[APP] Log file: predictions.log | Database: {DB_PATH}")

# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5005, debug=False, threaded=True)