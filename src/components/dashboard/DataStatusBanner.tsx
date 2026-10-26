import { WifiOff, AlertOctagon, FlaskConical, Hourglass } from 'lucide-react';
import { LiveData } from '@/types/sensor';

// 'offline' = backend unreachable, 'no-data' = backend up, no reading yet
export type LiveState = 'ok' | 'offline' | 'no-data';

interface DataStatusBannerProps {
  liveState: LiveState;
  data: LiveData | null;
}

function formatAge(seconds: number) {
  if (seconds < 120) return `${Math.round(seconds)} s`;
  if (seconds < 7200) return `${Math.round(seconds / 60)} min`;
  return `${Math.round(seconds / 3600)} h`;
}

// Tells the operator when the numbers on screen are NOT live machine data.
const DataStatusBanner = ({ liveState, data }: DataStatusBannerProps) => {
  let tone: 'critical' | 'warning';
  let Icon = AlertOctagon;
  let title: string;
  let detail: string;

  if (liveState === 'offline') {
    tone = 'critical';
    Icon = WifiOff;
    title = 'Backend unreachable';
    detail = 'The dashboard cannot reach the Edge AI backend (port 5005). Values below are not updating.';
  } else if (liveState === 'no-data') {
    tone = 'warning';
    Icon = Hourglass;
    title = 'Waiting for sensor data';
    detail = 'The backend is running but no sensor reading has arrived yet.';
  } else if (data?.stale) {
    tone = 'critical';
    title = `Sensor offline — last reading ${formatAge(data.age_s ?? 0)} ago`;
    detail = 'Values below are from the last reading, not live. Check sensor power, wiring and the MQTT link.';
  } else if (data?.simulated) {
    tone = 'warning';
    Icon = FlaskConical;
    title = 'Demo mode — simulated data';
    detail = 'Readings come from the built-in synthetic sensor. They do not describe a real machine and never trigger STOP/RUN.';
  } else {
    return null;
  }

  const styles = tone === 'critical'
    ? 'border-status-critical/40 bg-status-critical/15 status-critical'
    : 'border-status-warning/40 bg-status-warning/15 status-warning';

  // Sticky: the warning stays visible while scrolling through the charts
  return (
    <div className="sticky top-0 z-30 -mx-1 px-1 py-1 bg-background/90 backdrop-blur">
      <div role="alert" className={`flex items-start gap-3 rounded-xl border px-4 py-3 ${styles}`}>
        <Icon size={18} className="mt-0.5 shrink-0" />
        <div>
          <p className="text-sm font-semibold">{title}</p>
          <p className="text-xs text-foreground/80 mt-0.5">{detail}</p>
        </div>
      </div>
    </div>
  );
};

export default DataStatusBanner;
