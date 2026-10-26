import { useState, useEffect } from 'react';
import { ForecastData, Recommendation } from '@/types/sensor';
import { fetchForecast, fetchRecommendations } from '@/services/api';

const FORECAST_INTERVAL = 10000;

// Forecast + recommendations change slowly → polled every 10 s.
// The prediction itself arrives live through useLiveStream.
// No mock fallback: when the backend has nothing, panels show "no data".
export function useForecastData() {
  const [forecast, setForecast] = useState<ForecastData | null>(null);
  const [recommendations, setRecommendations] = useState<Recommendation[]>([]);

  useEffect(() => {
    const poll = () => {
      fetchForecast().then(setForecast).catch(() => setForecast(null));
      fetchRecommendations().then(setRecommendations).catch(() => setRecommendations([]));
    };
    poll();
    const interval = setInterval(poll, FORECAST_INTERVAL);
    return () => clearInterval(interval);
  }, []);

  return { forecast, recommendations };
}
