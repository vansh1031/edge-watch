import { useState, useEffect, useCallback, useRef } from 'react';
import { PredictionData, ForecastData, Recommendation } from '@/types/sensor';
import { fetchPrediction, fetchForecast, fetchRecommendations, ApiError } from '@/services/api';

const PREDICTION_INTERVAL = 4000;

// 'offline' = backend unreachable, 'no-data' = backend up but no sensor reading yet
export type PredictionState = 'ok' | 'offline' | 'no-data';

// No mock fallback: when the backend has nothing to say, the dashboard shows
// "no data" instead of invented predictions.
export function usePredictionData() {
  const [prediction, setPrediction] = useState<PredictionData | null>(null);
  const [forecast, setForecast] = useState<ForecastData | null>(null);
  const [recommendations, setRecommendations] = useState<Recommendation[]>([]);
  const [state, setState] = useState<PredictionState>('ok');
  const [isLoading, setIsLoading] = useState(true);

  const intervalRef = useRef<ReturnType<typeof setInterval>>();

  const poll = useCallback(async () => {
    try {
      const [rawPred, rawFc, rawRecs] = await Promise.all([
        fetchPrediction(),
        fetchForecast(),
        fetchRecommendations().catch(() => [] as Recommendation[]),
      ]);

      setPrediction({
        ...rawPred,
        failure_risk: Number(rawPred.failure_risk ?? 0),
        rul_hours:    Number(rawPred.rul_hours ?? 0),
        confidence:   Number(rawPred.confidence ?? 0),
        health_score: Number(rawPred.health_score ?? 100 - Number(rawPred.failure_risk ?? 0)),
      });
      setForecast(rawFc);
      setRecommendations(rawRecs);
      setState('ok');
    } catch (err) {
      setPrediction(null);
      setForecast(null);
      setRecommendations([]);
      setState(err instanceof ApiError && err.status === 503 ? 'no-data' : 'offline');
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    poll();
    intervalRef.current = setInterval(poll, PREDICTION_INTERVAL);
    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current);
    };
  }, [poll]);

  return { prediction, forecast, recommendations, state, isLoading };
}
