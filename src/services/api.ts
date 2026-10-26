import { SensorData, AnomalyResponse, PredictionData, ForecastData, Alert, Recommendation } from '@/types/sensor';

// Backend runs on port 5005 of the same machine that serves the dashboard,
// so this works both on the Jetson itself and from other PCs on the network.
export const API_BASE =
  import.meta.env.VITE_API_BASE_URL ||
  `${window.location.protocol}//${window.location.hostname}:5005`;

async function fetchWithTimeout(url: string, timeout = 5000): Promise<Response> {
  const controller = new AbortController();
  const id = setTimeout(() => controller.abort(), timeout);
  try {
    const response = await fetch(url, { signal: controller.signal });
    clearTimeout(id);
    return response;
  } catch {
    clearTimeout(id);
    throw new Error('Connection failed');
  }
}

export async function fetchLiveData(): Promise<SensorData> {
  const res = await fetchWithTimeout(`${API_BASE}/api/live-data`);
  if (!res.ok) throw new Error('Failed to fetch live data');
  return res.json();
}

export async function fetchHistory(params?: {
  startDate?: string;
  endDate?: string;
  page?: number;
  limit?: number;
  search?: string;
}): Promise<{ data: SensorData[]; total: number }> {
  const query = new URLSearchParams();
  if (params?.startDate) query.set('start_date', params.startDate);
  if (params?.endDate) query.set('end_date', params.endDate);
  if (params?.page) query.set('page', String(params.page));
  if (params?.limit) query.set('limit', String(params.limit));
  if (params?.search) query.set('search', params.search);
  const res = await fetchWithTimeout(`${API_BASE}/api/history?${query}`);
  if (!res.ok) throw new Error('Failed to fetch history');
  return res.json();
}

export async function fetchAnomaly(): Promise<AnomalyResponse> {
  const res = await fetchWithTimeout(`${API_BASE}/api/anomaly`);
  if (!res.ok) throw new Error('Failed to fetch anomaly');
  return res.json();
}

export async function fetchPrediction(): Promise<PredictionData> {
  const res = await fetchWithTimeout(`${API_BASE}/api/prediction`);
  if (!res.ok) throw new Error('Failed to fetch prediction');
  return res.json();
}

export async function fetchForecast(): Promise<ForecastData> {
  const res = await fetchWithTimeout(`${API_BASE}/api/forecast`);
  if (!res.ok) throw new Error('Failed to fetch forecast');
  return res.json();
}

export async function fetchAlerts(limit = 20): Promise<Alert[]> {
  const res = await fetchWithTimeout(`${API_BASE}/api/alerts?limit=${limit}`);
  if (!res.ok) throw new Error('Failed to fetch alerts');
  const raw: any[] = await res.json();
  return raw
    .map(a => ({
      id: String(a.id ?? `${a.type}-${a.timestamp}`),
      message: String(a.message ?? ''),
      severity: a.severity === 'critical' || a.severity === 'warning' ? a.severity : 'normal',
      timestamp: new Date(Number(a.timestamp)),
    }))
    .reverse();
}

export async function fetchRecommendations(): Promise<Recommendation[]> {
  const res = await fetchWithTimeout(`${API_BASE}/api/recommendations`);
  if (!res.ok) throw new Error('Failed to fetch recommendations');
  return res.json();
}
