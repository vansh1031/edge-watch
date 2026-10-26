import { useState } from 'react';
import { AlertTriangle, AlertCircle, Info, Clock, CheckCircle2 } from 'lucide-react';
import { toast } from 'sonner';
import { Alert } from '@/types/sensor';
import { ackAlert } from '@/services/api';
import { readOperator, saveOperator } from '@/lib/operator';

interface AlertPanelProps {
  alerts: Alert[];
  onAcknowledged?: (alert: Alert) => void;
}

const severityConfig = {
  critical: { icon: AlertCircle, className: 'status-critical bg-status-critical/15' },
  warning: { icon: AlertTriangle, className: 'status-warning bg-status-warning/15' },
  normal: { icon: Info, className: 'text-primary bg-primary/10' },
};

const formatTime = (d: Date) => d.toLocaleTimeString('en-US', { hour12: false });

const AlertPanel = ({ alerts, onAcknowledged }: AlertPanelProps) => {
  const [openOnly, setOpenOnly] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);

  const needsAck = (a: Alert) => a.severity !== 'normal' && !a.acknowledged;
  const openCount = alerts.filter(needsAck).length;
  const shown = openOnly ? alerts.filter(needsAck) : alerts;

  const acknowledge = async (alert: Alert) => {
    let operator = readOperator();
    if (!operator) {
      operator = (window.prompt('Your name (recorded with the acknowledgement):') ?? '').trim();
      if (!operator) return;
      saveOperator(operator);
    }
    setBusyId(alert.id);
    try {
      onAcknowledged?.(await ackAlert(alert.id, operator));
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Acknowledge failed');
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="glow-card rounded-xl bg-card p-5 h-full">
      <div className="flex items-center gap-2 mb-4">
        <AlertTriangle size={14} className="text-primary" />
        <h3 className="text-sm font-semibold text-foreground">Alerts</h3>
        {openCount > 0 && (
          <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-status-critical/15 status-critical font-semibold">
            {openCount} open
          </span>
        )}
        <label className="ml-auto flex items-center gap-1.5 text-[11px] text-muted-foreground cursor-pointer">
          <input type="checkbox" checked={openOnly} onChange={e => setOpenOnly(e.target.checked)} />
          Open only
        </label>
      </div>
      <div className="space-y-3 max-h-[500px] overflow-y-auto scrollbar-thin pr-1">
        {shown.length === 0 ? (
          <p className="text-xs text-muted-foreground text-center py-8">
            {openOnly ? 'No open alerts' : 'No alerts'}
          </p>
        ) : (
          shown.map((alert) => {
            const config = severityConfig[alert.severity];
            const Icon = config.icon;
            return (
              <div
                key={alert.id}
                className={`flex items-start gap-3 p-3 rounded-lg border border-border transition-all duration-300 hover:border-border/80 ${
                  alert.acknowledged ? 'opacity-60' : ''
                }`}
              >
                <div className={`p-1 rounded-md ${config.className}`}>
                  <Icon size={12} />
                </div>
                <div className="flex-1 min-w-0">
                  <p className="text-xs font-medium text-foreground leading-relaxed">
                    {alert.simulated && (
                      <span className="mr-1.5 text-[9px] px-1 py-0.5 rounded bg-status-warning/15 status-warning font-semibold align-middle">DEMO</span>
                    )}
                    {alert.message}
                  </p>
                  <div className="flex items-center gap-1 mt-1 text-muted-foreground">
                    <Clock size={10} />
                    <span className="text-[10px] font-mono">{formatTime(alert.timestamp)}</span>
                  </div>
                  {alert.acknowledged ? (
                    <p className="mt-1 flex items-center gap-1 text-[10px] status-normal">
                      <CheckCircle2 size={10} />
                      Acknowledged by {alert.ack_by}
                      {alert.ack_ts ? ` · ${formatTime(new Date(alert.ack_ts))}` : ''}
                    </p>
                  ) : needsAck(alert) && (
                    <button
                      onClick={() => acknowledge(alert)}
                      disabled={busyId === alert.id}
                      className="mt-1.5 text-[10px] px-2 py-0.5 rounded-md border border-border text-foreground hover:bg-secondary disabled:opacity-50 transition-colors"
                    >
                      {busyId === alert.id ? 'Saving…' : 'Acknowledge'}
                    </button>
                  )}
                </div>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
};

export default AlertPanel;
