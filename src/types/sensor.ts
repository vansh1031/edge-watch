// ─── Sensor Data ──────────────────────────────────────────────
export interface SensorData {
  timestamp:    number;
  seq?:         number;
  key?:         string;
  aRMSx:        number;
  aRMSy:        number;
  aRMSz:        number;
  vRMSx:        number;
  vRMSy:        number;
  vRMSz:        number;
  temperature:  number;
  aucausticRMS: number;
  acousticRMS?: number;
  mac?:         string;
  extra?:       string;
}

// ─── Status ───────────────────────────────────────────────────
export type MachineStatus = 'normal' | 'warning' | 'critical';

// ─── Data freshness (sent by backend with live data + prediction) ──
export interface DataFreshness {
  simulated: boolean;        // reading comes from the built-in synthetic sensor
  stale:     boolean;        // real sensor silent longer than EDGEWATCH_SENSOR_TIMEOUT
  age_s:     number | null;  // seconds since last real reading
}

// ─── Live reading (/api/live-data) ────────────────────────────
export interface LiveData extends SensorData, DataFreshness {
  levels: Partial<Record<keyof SensorData, MachineStatus>>;  // backend thresholds per field
}

// ─── Machine control (/api/control/status) ────────────────────
export interface ControlStatus {
  enabled:        boolean;
  auto_resume:    boolean;
  manual_resume:  boolean;          // CONTROL_TOKEN configured on the backend
  holding_stop:   boolean;          // last command sent was STOP
  last_cmd:       'STOP' | 'RUN' | null;
  last_cmd_ts:    number | null;
  last_reason:    string | null;
  mqtt_connected: boolean;
  machine_status: MachineStatus | null;
  motor_off:      boolean | null;
}

// ─── Alerts ───────────────────────────────────────────────────
export interface Alert {
  id:        string;
  message:   string;
  severity:  MachineStatus;
  timestamp: Date;
}

// ─── Anomaly ──────────────────────────────────────────────────
export interface AnomalyResponse {
  anomaly:     boolean;
  status:      MachineStatus;
  healthScore: number;
  alerts:      Alert[];
}

// ─── Fault Types ──────────────────────────────────────────────
export type FaultType = 'bearing fault' | 'misalignment' | 'imbalance' | 'overheating' | 'none';

// ─── Prediction (FINAL FIXED) ─────────────────────────────────
export interface PredictionData extends Partial<DataFreshness> {
  failure_risk: number;   // %
  rul_hours: number;      // hours
  status: MachineStatus;
  fault_type: FaultType;
  confidence: number;     // 0–1

  // ✅ MAIN FIELD (important)
  health_score: number;   // 0–100
  motor_off?: boolean;
}

// ─── Forecast ─────────────────────────────────────────────────
export interface ForecastPoint {
  timestamp: number;
  value:     number;
  predicted: boolean;
}

export interface ForecastData {
  vRMSy:       ForecastPoint[];
  temperature: ForecastPoint[];
  healthScore: ForecastPoint[];
}

// ─── Recommendations ──────────────────────────────────────────
export interface Recommendation {
  id:       string;
  message:  string;
  priority: 'high' | 'medium' | 'low';
  category: 'maintenance' | 'risk' | 'optimization';
}