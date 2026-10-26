import { useState, useEffect } from 'react';
import { Thresholds } from '@/types/sensor';
import { fetchThresholds } from '@/services/api';

// Warning / critical limits from the backend — the same ones used for alerts.
export function useThresholds() {
  const [thresholds, setThresholds] = useState<Thresholds>({});
  useEffect(() => {
    fetchThresholds().then(setThresholds).catch(() => { /* table falls back to no colouring */ });
  }, []);
  return thresholds;
}
