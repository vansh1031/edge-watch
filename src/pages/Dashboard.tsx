import { useState, useEffect, useCallback } from "react";
import TopBar from '@/components/dashboard/TopBar';
import StatusCards from '@/components/dashboard/StatusCards';
import SensorChart from '@/components/dashboard/SensorChart';
import AlertPanel from '@/components/dashboard/AlertPanel';
import LiveDataTable from '@/components/dashboard/LiveDataTable';
import { useForecastData } from '@/hooks/useForecastData';
import { useLiveStream } from '@/hooks/useLiveStream';
import { useThresholds } from '@/hooks/useThresholds';
import PredictionCard from '@/components/dashboard/PredictionCard';
import ForecastChart from '@/components/dashboard/ForecastChart';
import HealthTrend from '@/components/dashboard/HealthTrend';
import FaultDetection from '@/components/dashboard/FaultDetection';
import RecommendationPanel from '@/components/dashboard/RecommendationPanel';
import ControlPanel from '@/components/dashboard/ControlPanel';
import DataStatusBanner from '@/components/dashboard/DataStatusBanner';
import { API_BASE } from '@/services/api';
import { ForecastPoint, SensorData } from '@/types/sensor';

// ─── Chart line configs ────────────────────────────────────────
const velocityLines = [
  { key: 'vRMSx' as const, color: '#3b82f6', label: 'vRMS-X' },
  { key: 'vRMSy' as const, color: '#22d3ee', label: 'vRMS-Y' },
  { key: 'vRMSz' as const, color: '#a78bfa', label: 'vRMS-Z' },
];
const accelLines = [
  { key: 'aRMSx' as const, color: '#f59e0b', label: 'aRMS-X' },
  { key: 'aRMSy' as const, color: '#ef4444', label: 'aRMS-Y' },
  { key: 'aRMSz' as const, color: '#10b981', label: 'aRMS-Z' },
];
const tempLines = [
  { key: 'temperature' as const, color: '#f97316', label: 'Temperature' },
];
const acousticLines = [
  { key: 'aucausticRMS' as const, color: '#8b5cf6', label: 'Acoustic RMS' },
];
// ──────────────────────────────────────────────────────────────

const RANGE_LIMITS: Record<string, number> = {
  "1m": 60,
  "5m": 300,
  "1h": 1000,
};

const Dashboard = () => {

  // ─── Live stream: reading, prediction, alerts, machine control ─
  const { liveState, data, prediction, control, setControl, alerts, updateAlert } = useLiveStream();
  const { forecast, recommendations } = useForecastData();
  const thresholds = useThresholds();
  const isLoading = liveState === 'ok' && !prediction;

  const [history, setHistory] = useState<SensorData[]>([]);
  const [range, setRange] = useState("5m");
  const [rangeLoading, setRangeLoading] = useState(false);

  // ✅ NEW: Health History (FIX)
  const [healthHistory, setHealthHistory] = useState<ForecastPoint[]>([]);

  // ─── Fetch History ──────────────────────────────────────────
  const fetchHistory = useCallback((selectedRange: string) => {
    setRangeLoading(true);
    const limit = RANGE_LIMITS[selectedRange] || 1000;

    fetch(`${API_BASE}/api/history?range=${selectedRange}&limit=${limit}`)
      .then(res => res.json())
      .then((res: SensorData[]) => {
        if (!Array.isArray(res)) return;

        const formatted = res
          .map(item => ({ ...item, timestamp: Number(item.timestamp) }))
          .sort((a, b) => a.timestamp - b.timestamp);

        setHistory(formatted);
      })
      .catch(err => console.error("History error:", err))
      .finally(() => setRangeLoading(false));
  }, []);

  useEffect(() => {
    fetchHistory(range);
  }, [range, fetchHistory]);

  // ─── Append each streamed reading to the charts ─────────────
  useEffect(() => {
    if (!data || typeof data.temperature !== 'number') return;
    setHistory(prev => {
      const lastTs = prev.length > 0 ? prev[prev.length - 1].timestamp : 0;
      if (data.timestamp <= lastTs) return prev;

      const rangeMs: Record<string, number> = {
        "1m": 60_000,
        "5m": 300_000,
        "1h": 3_600_000,
      };
      const cutoff = Date.now() - (rangeMs[range] ?? Infinity);
      const updated = [...prev, data].filter(p => p.timestamp >= cutoff);
      return updated.length > 1000 ? updated.slice(-1000) : updated;
    });
  }, [data, range]);

  // ✅ NEW: Health Trend Fix (MAIN LOGIC)
  useEffect(() => {
    if (prediction && typeof prediction.health_score === "number") {
      setHealthHistory(prev => [
        ...prev.slice(-50),
        {
          timestamp: Date.now(),
          value: prediction.health_score,
          predicted: false
        }
      ]);
    }
  }, [prediction]);
  return (
    <div className="min-h-screen bg-background">

      <TopBar isConnected={liveState !== 'offline'} useMock={Boolean(data?.simulated)} />

      <main className="p-4 lg:p-6 space-y-6 max-w-[1600px] mx-auto">

        <DataStatusBanner liveState={liveState} data={data} />

        {/* ── Time Range Selector ── */}
        <div className="flex items-center gap-3">
          <span className="text-xs text-muted-foreground uppercase tracking-wide font-semibold">
            Time Range:
          </span>

          <div className="flex gap-2">
            {["1m", "5m", "1h", "all"].map(r => (
              <button
                key={r}
                onClick={() => setRange(r)}
                disabled={rangeLoading}
                className={`px-4 py-1.5 rounded text-xs font-semibold uppercase tracking-wide border transition-all
                  ${range === r
                    ? "bg-accent text-white border-accent"
                    : "text-muted-foreground border-border hover:border-accent"}
                  ${rangeLoading ? "opacity-50 cursor-wait" : ""}`}
              >
                {r}
              </button>
            ))}
          </div>

          <span className="text-xs text-muted-foreground ml-2">
            {rangeLoading ? "Loading..." : `${history.length} points`}
          </span>
        </div>

        {/* ── Status Cards ── */}
        <StatusCards data={data} anomaly={prediction} isLoading={isLoading} />

        {/* ── Prediction ── */}
        <div>
          <h2 className="text-sm font-semibold text-accent uppercase mb-3">
            AI Predictive Maintenance
          </h2>
          <PredictionCard data={prediction} isLoading={isLoading} />
        </div>

        {/* ── Forecast + Health Trend ── */}
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">

          <ForecastChart
            data={forecast?.vRMSy || []}
            title="Vibration Forecast"
            color="#22d3ee"
          />

          <ForecastChart
            data={forecast?.temperature || []}
            title="Temperature Forecast"
            color="#f97316"
          />

          {/* ✅ FIXED HEALTH GRAPH */}
          <HealthTrend
            data={(forecast?.healthScore?.length ?? 0) >= 2 ? forecast!.healthScore : healthHistory}
          />

        </div>

        {/* ── Fault + Recommendations + Machine Control ── */}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          <FaultDetection data={prediction} />
          <RecommendationPanel data={recommendations} />
          <ControlPanel control={control} onChanged={setControl} />
        </div>

        {/* ── Live Charts ── */}
        <div className="grid grid-cols-1 lg:grid-cols-4 gap-6">

          <div className="lg:col-span-3 space-y-6">
            <div className="grid grid-cols-1 md:grid-cols-2 gap-6">

              <SensorChart data={history} title="Velocity RMS" lines={velocityLines} />
              <SensorChart data={history} title="Acceleration RMS" lines={accelLines} />
              <SensorChart data={history} title="Temperature" lines={tempLines} />
              <SensorChart data={history} title="Acoustic RMS" lines={acousticLines} />

            </div>

            <LiveDataTable data={history} thresholds={thresholds} />
          </div>

          <div className="lg:col-span-1">
            <AlertPanel alerts={alerts} onAcknowledged={updateAlert} />
          </div>

        </div>

      </main>
    </div>
  );
};

export default Dashboard;