import { useState, useEffect, useCallback } from 'react';
import { Alert, ControlStatus, LiveData, PredictionData, RawAlert, StreamStatus } from '@/types/sensor';
import { STREAM_URL, fetchAlerts, toAlert } from '@/services/api';
import { LiveState } from '@/components/dashboard/DataStatusBanner';

const MAX_ALERTS = 50;

/**
 * One Server-Sent Events connection (/api/stream) instead of polling.
 * Delivers the latest reading, ML prediction, new alerts and a 2 s heartbeat
 * (freshness + machine control state). EventSource reconnects by itself.
 */
export function useLiveStream() {
  const [liveState, setLiveState] = useState<LiveState>('ok');
  const [data, setData] = useState<LiveData | null>(null);
  const [prediction, setPrediction] = useState<PredictionData | null>(null);
  const [control, setControl] = useState<ControlStatus | null>(null);
  const [alerts, setAlerts] = useState<Alert[]>([]);

  const loadAlerts = useCallback(() => {
    fetchAlerts(MAX_ALERTS).then(setAlerts).catch(() => { /* stream error state covers this */ });
  }, []);

  useEffect(() => {
    const source = new EventSource(STREAM_URL);
    const parse = <T,>(e: Event) => JSON.parse((e as MessageEvent).data) as T;

    source.onopen = () => loadAlerts();   // (re)connected → catch up on alerts missed meanwhile
    source.onerror = () => {
      setLiveState('offline');
      setControl(null);
    };

    source.addEventListener('status', e => {
      const status = parse<StreamStatus>(e);
      setLiveState(status.has_data ? 'ok' : 'no-data');
      setControl(status.control);
      if (!status.has_data) setPrediction(null);
      // Heartbeat keeps "last reading X s ago" moving while the sensor is silent
      setData(prev => (prev && status.has_data
        ? { ...prev, stale: Boolean(status.stale), age_s: status.age_s ?? null }
        : prev));
    });
    source.addEventListener('reading', e => {
      const reading = parse<LiveData>(e);
      setData({ ...reading, timestamp: Number(reading.timestamp) });
      setLiveState('ok');
    });
    source.addEventListener('prediction', e => setPrediction(parse<PredictionData>(e)));
    source.addEventListener('alert', e => {
      const alert = toAlert(parse<RawAlert>(e));
      setAlerts(prev => [alert, ...prev.filter(a => a.id !== alert.id)].slice(0, MAX_ALERTS));
    });
    source.addEventListener('alert_ack', e => {
      const alert = toAlert(parse<RawAlert>(e));
      setAlerts(prev => prev.map(a => (a.id === alert.id ? alert : a)));
    });

    return () => source.close();
  }, [loadAlerts]);

  // Local update after this browser acknowledged an alert / resumed the machine
  const updateAlert = useCallback((alert: Alert) => {
    setAlerts(prev => prev.map(a => (a.id === alert.id ? alert : a)));
  }, []);

  return { liveState, data, prediction, control, setControl, alerts, updateAlert };
}
