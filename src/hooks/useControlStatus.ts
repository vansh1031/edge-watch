import { useState, useEffect, useCallback } from 'react';
import { ControlStatus } from '@/types/sensor';
import { fetchControlStatus } from '@/services/api';

const CONTROL_INTERVAL = 3000;

export function useControlStatus() {
  const [control, setControl] = useState<ControlStatus | null>(null);

  const refresh = useCallback(() => {
    fetchControlStatus().then(setControl).catch(() => setControl(null));
  }, []);

  useEffect(() => {
    refresh();
    const interval = setInterval(refresh, CONTROL_INTERVAL);
    return () => clearInterval(interval);
  }, [refresh]);

  return { control, refresh };
}
