import { SensorData, ForecastData, Alert, RawAlert, Recommendation, ControlStatus, Thresholds } from '@/types/sensor';

// Backend runs on port 5005 of the same machine that serves the dashboard,
// so this works both on the Jetson itself and from other PCs on the network.
export const API_BASE =
  import.meta.env.VITE_API_BASE_URL ||
  `${window.location.protocol}//${window.location.hostname}:5005`;

// status 0 = backend unreachable, 503 = backend up but no sensor reading yet
export class ApiError extends Error {
  constructor(message: string, public status: number) {
    super(message);
  }
}

async function fetchWithTimeout(url: string, timeout = 5000, init?: RequestInit): Promise<Response> {
  const controller = new AbortController();
  const id = setTimeout(() => controller.abort(), timeout);
  try {
    const response = await fetch(url, { ...init, signal: controller.signal });
    clearTimeout(id);
    return response;
  } catch {
    clearTimeout(id);
    throw new ApiError('Connection failed', 0);
  }
}

export interface HistoryQuery {
  startMs?: number;
  endMs?: number;
  q?: string;
  page?: number;
  limit?: number;
}

function historyParams(params: HistoryQuery) {
  const query = new URLSearchParams();
  if (params.startMs !== undefined) query.set('start_ms', String(params.startMs));
  if (params.endMs !== undefined) query.set('end_ms', String(params.endMs));
  if (params.q) query.set('q', params.q);
  if (params.page) query.set('page', String(params.page));
  if (params.limit) query.set('limit', String(params.limit));
  return query;
}

// History page: filtered + paginated on the server (real readings only)
export async function searchHistory(params: HistoryQuery): Promise<{ data: SensorData[]; total: number }> {
  const res = await fetchWithTimeout(`${API_BASE}/api/history/search?${historyParams(params)}`, 15000);
  if (!res.ok) throw new ApiError('Failed to fetch history', res.status);
  return res.json();
}

export function historyExportUrl(params: Pick<HistoryQuery, 'startMs' | 'endMs'>) {
  return `${API_BASE}/api/history/export.csv?${historyParams(params)}`;
}

export async function fetchForecast(): Promise<ForecastData> {
  const res = await fetchWithTimeout(`${API_BASE}/api/forecast`);
  if (!res.ok) throw new Error('Failed to fetch forecast');
  return res.json();
}

export function toAlert(a: RawAlert): Alert {
  return {
    id: String(a.id),
    type: a.type,
    message: String(a.message ?? ''),
    severity: a.severity === 'critical' || a.severity === 'warning' ? a.severity : 'normal',
    timestamp: new Date(Number(a.timestamp)),
    simulated: Boolean(a.simulated),
    acknowledged: Boolean(a.acknowledged),
    ack_by: a.ack_by ?? null,
    ack_ts: a.ack_ts ?? null,
  };
}

// Newest first
export async function fetchAlerts(limit = 20): Promise<Alert[]> {
  const res = await fetchWithTimeout(`${API_BASE}/api/alerts?limit=${limit}`);
  if (!res.ok) throw new Error('Failed to fetch alerts');
  const raw: RawAlert[] = await res.json();
  return raw.map(toAlert);
}

export async function ackAlert(id: string, operator: string): Promise<Alert> {
  const res = await fetchWithTimeout(`${API_BASE}/api/alerts/${id}/ack`, 5000, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ operator }),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new ApiError(body.error ?? `Acknowledge failed (HTTP ${res.status})`, res.status);
  return toAlert(body);
}

export async function fetchThresholds(): Promise<Thresholds> {
  const res = await fetchWithTimeout(`${API_BASE}/api/thresholds`);
  if (!res.ok) throw new ApiError('Failed to fetch thresholds', res.status);
  return res.json();
}

export const STREAM_URL = `${API_BASE}/api/stream`;

export async function fetchRecommendations(): Promise<Recommendation[]> {
  const res = await fetchWithTimeout(`${API_BASE}/api/recommendations`);
  if (!res.ok) throw new Error('Failed to fetch recommendations');
  return res.json();
}

// Sends RUN to the machine. Backend refuses while the machine is still critical.
export async function resumeMachine(token: string, operator: string): Promise<ControlStatus> {
  const res = await fetchWithTimeout(`${API_BASE}/api/control/resume`, 5000, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Control-Token': token },
    body: JSON.stringify({ operator }),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new ApiError(body.error ?? `Resume failed (HTTP ${res.status})`, res.status);
  return body.control;
}
