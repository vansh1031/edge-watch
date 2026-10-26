import { useState } from 'react';
import { Power, OctagonX, PlayCircle, Radio, Lock } from 'lucide-react';
import { toast } from 'sonner';
import { Dialog, DialogContent, DialogTitle, DialogDescription } from '@/components/ui/dialog';
import { ControlStatus } from '@/types/sensor';
import { resumeMachine } from '@/services/api';

interface ControlPanelProps {
  control: ControlStatus | null;
  onChanged: () => void;
}

const OPERATOR_KEY = 'edgewatch.operator';

function readOperator() {
  try { return localStorage.getItem(OPERATOR_KEY) ?? ''; } catch { return ''; }
}

function saveOperator(name: string) {
  try { localStorage.setItem(OPERATOR_KEY, name); } catch { /* storage blocked */ }
}

function formatTime(ts: number | null) {
  return ts ? new Date(ts).toLocaleString('en-US', { hour12: false }) : '—';
}

const inputClass =
  'w-full bg-secondary border border-border rounded-lg px-3 py-2 text-sm text-foreground ' +
  'placeholder-muted-foreground focus:outline-none focus:ring-1 focus:ring-primary';

const ControlPanel = ({ control, onChanged }: ControlPanelProps) => {
  const [open, setOpen] = useState(false);
  const [operator, setOperator] = useState(readOperator);
  const [token, setToken] = useState('');
  const [checked, setChecked] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const stillCritical = control?.machine_status === 'critical' && !control?.motor_off;
  const canResume = Boolean(control?.holding_stop && control.manual_resume && !stillCritical);

  const openDialog = () => {
    setToken('');
    setChecked(false);
    setError(null);
    setOpen(true);
  };

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      await resumeMachine(token, operator.trim());
      saveOperator(operator.trim());
      toast.success('RUN command sent — machine resumed');
      setOpen(false);
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Resume failed');
    } finally {
      setBusy(false);
    }
  };

  // ── Panel body ────────────────────────────────────────────────
  let body;
  if (!control) {
    body = <p className="text-xs text-muted-foreground py-4 text-center">Control status unavailable — backend unreachable.</p>;
  } else if (!control.enabled) {
    body = <p className="text-xs text-muted-foreground py-4 text-center">Automatic shutdown is disabled (SHUTDOWN_ENABLED=false).</p>;
  } else {
    const holding = control.holding_stop;
    body = (
      <div className="space-y-3">
        <div className={`flex items-center gap-3 rounded-lg border p-3 ${holding ? 'border-status-critical/40 bg-status-critical/15' : 'border-status-normal/30 bg-status-normal/15'}`}>
          {holding
            ? <OctagonX size={22} className="status-critical shrink-0" />
            : <Power size={22} className="status-normal shrink-0" />}
          <div className="min-w-0">
            <p className={`font-bold ${holding ? 'status-critical' : 'status-normal'}`}>
              {holding ? 'STOP active — machine held stopped' : 'No STOP active'}
            </p>
            <p className="text-xs text-muted-foreground truncate">
              {control.last_cmd
                ? `Last command: ${control.last_cmd} · ${formatTime(control.last_cmd_ts)}`
                : 'No command sent since backend start'}
            </p>
          </div>
        </div>

        {control.last_reason && (
          <p className="text-xs text-muted-foreground font-mono break-words">{control.last_reason}</p>
        )}

        <div className="flex items-center gap-1.5 text-xs">
          <Radio size={12} className={control.mqtt_connected ? 'status-normal' : 'status-critical'} />
          <span className={control.mqtt_connected ? 'text-muted-foreground' : 'status-critical font-semibold'}>
            {control.mqtt_connected ? 'MQTT connected' : 'MQTT disconnected — STOP commands cannot be delivered!'}
          </span>
        </div>

        {holding && (
          <>
            <button
              onClick={openDialog}
              disabled={!canResume}
              className="w-full flex items-center justify-center gap-2 px-4 py-2 rounded-lg text-sm font-semibold bg-accent text-white hover:bg-accent/90 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
            >
              <PlayCircle size={16} /> Resume machine
            </button>
            {!control.manual_resume && (
              <p className="text-[11px] text-muted-foreground flex items-center gap-1">
                <Lock size={11} /> Manual resume is off — set CONTROL_TOKEN in backend/.env
              </p>
            )}
            {stillCritical && (
              <p className="text-[11px] status-critical">Machine is still critical — resume is blocked.</p>
            )}
          </>
        )}
      </div>
    );
  }

  return (
    <div className="glow-card rounded-xl bg-card p-5">
      <div className="flex items-center gap-2 mb-4">
        <Power size={16} className="text-accent" />
        <h3 className="text-sm font-semibold text-foreground">Machine Control</h3>
        {control?.auto_resume && (
          <span className="ml-auto text-[10px] px-1.5 py-0.5 rounded-full bg-muted text-muted-foreground uppercase">auto-resume on</span>
        )}
      </div>
      {body}

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="bg-card border-border max-w-md">
          <DialogTitle>Resume machine?</DialogTitle>
          <DialogDescription>
            This sends RUN on the control topic and the motor may start immediately.
          </DialogDescription>

          <form
            className="space-y-3 mt-2"
            onSubmit={e => { e.preventDefault(); submit(); }}
          >
            <label className="block space-y-1">
              <span className="text-xs text-muted-foreground">Your name</span>
              <input className={inputClass} value={operator} onChange={e => setOperator(e.target.value)} required autoComplete="name" />
            </label>
            <label className="block space-y-1">
              <span className="text-xs text-muted-foreground">Control token</span>
              <input className={inputClass} type="password" value={token} onChange={e => setToken(e.target.value)} required autoComplete="off" />
            </label>
            <label className="flex items-start gap-2 text-xs text-foreground">
              <input type="checkbox" className="mt-0.5" checked={checked} onChange={e => setChecked(e.target.checked)} />
              I have checked the machine and the area around it — it is safe to restart.
            </label>

            {error && <p className="text-xs status-critical">{error}</p>}

            <div className="flex justify-end gap-2 pt-1">
              <button type="button" onClick={() => setOpen(false)} className="px-4 py-2 rounded-lg text-sm bg-secondary text-foreground hover:bg-secondary/80">
                Cancel
              </button>
              <button
                type="submit"
                disabled={!checked || !token || !operator.trim() || busy}
                className="px-4 py-2 rounded-lg text-sm font-semibold bg-accent text-white hover:bg-accent/90 disabled:opacity-40"
              >
                {busy ? 'Sending…' : 'Send RUN'}
              </button>
            </div>
          </form>
        </DialogContent>
      </Dialog>
    </div>
  );
};

export default ControlPanel;
