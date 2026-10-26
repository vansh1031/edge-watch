// Remembered operator name for acknowledgements and machine resume (per browser).
const OPERATOR_KEY = 'edgewatch.operator';

export function readOperator(): string {
  try { return localStorage.getItem(OPERATOR_KEY) ?? ''; } catch { return ''; }
}

export function saveOperator(name: string) {
  try { localStorage.setItem(OPERATOR_KEY, name); } catch { /* storage blocked */ }
}
