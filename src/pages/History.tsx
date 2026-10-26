import { useState, useEffect } from 'react';
import { Search, Calendar, ChevronLeft, ChevronRight, WifiOff, Download } from 'lucide-react';
import { SensorData } from '@/types/sensor';
import { searchHistory, historyExportUrl } from '@/services/api';
import TopBar from '@/components/dashboard/TopBar';

const ITEMS_PER_PAGE = 20;

const formatDateTime = (ts: number) => {
  const d = new Date(ts);
  return d.toLocaleString('en-US', { hour12: false });
};

// "2026-10-26" → local midnight in ms (new Date("2026-10-26") would be UTC midnight)
function localDayStart(date: string): number | undefined {
  if (!date) return undefined;
  const [y, m, d] = date.split('-').map(Number);
  return new Date(y, m - 1, d).getTime();
}

const History = () => {
  const [rows, setRows] = useState<SensorData[]>([]);
  const [total, setTotal] = useState(0);
  const [isLive, setIsLive] = useState(false);
  const [loadError, setLoadError] = useState(false);
  const [loading, setLoading] = useState(true);

  const [search, setSearch] = useState('');
  const [query, setQuery] = useState('');          // debounced search
  const [startDate, setStartDate] = useState('');
  const [endDate, setEndDate] = useState('');
  const [page, setPage] = useState(1);

  const startMs = localDayStart(startDate);
  const endDayStart = localDayStart(endDate);
  const endMs = endDayStart !== undefined ? endDayStart + 86_400_000 : undefined;   // end date is inclusive

  useEffect(() => {
    const t = setTimeout(() => setQuery(search.trim()), 300);
    return () => clearTimeout(t);
  }, [search]);

  useEffect(() => { setPage(1); }, [query, startDate, endDate]);

  // Filtering + paging happen on the backend (SQLite) — only one page is downloaded
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    searchHistory({ startMs, endMs, q: query, page, limit: ITEMS_PER_PAGE })
      .then(res => {
        if (cancelled) return;
        setRows(res.data);
        setTotal(res.total);
        setIsLive(true);
        setLoadError(false);
      })
      // No mock fallback — an empty table with an error is honest, fake rows are not
      .catch(() => { if (!cancelled) setLoadError(true); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [startMs, endMs, query, page]);

  const totalPages = Math.ceil(total / ITEMS_PER_PAGE);

  const columns = [
    'timestamp', 'aRMSx', 'aRMSy', 'aRMSz', 'vRMSx', 'vRMSy', 'vRMSz', 'temperature', 'aucausticRMS',
  ] as const;

  return (
    <div className="min-h-screen bg-background">
      <TopBar isConnected={isLive && !loadError} useMock={false} />

      <main className="p-4 lg:p-6 max-w-[1600px] mx-auto space-y-6">
        <div className="flex items-center justify-between">
          <h2 className="text-xl font-bold">
            <span className="text-primary glow-text">History</span>
            <span className="text-muted-foreground ml-2 text-sm font-normal">Sensor Data Records</span>
          </h2>
          <a
            href={historyExportUrl({ startMs, endMs })}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium bg-secondary text-foreground hover:bg-secondary/80 transition-colors"
          >
            <Download size={13} /> Export CSV
          </a>
        </div>
        <p className="text-xs text-muted-foreground -mt-4">
          Real sensor readings stored in the database. Demo (simulated) data is never stored. Export uses the selected dates.
        </p>

        {loadError && (
          <div role="alert" className="flex items-start gap-3 rounded-xl border border-status-critical/40 bg-status-critical/15 status-critical px-4 py-3">
            <WifiOff size={18} className="mt-0.5 shrink-0" />
            <div>
              <p className="text-sm font-semibold">Could not load history</p>
              <p className="text-xs text-foreground/80 mt-0.5">The Edge AI backend (port 5005) is unreachable. Reload the page when it is running.</p>
            </div>
          </div>
        )}

        <div className="glow-card rounded-xl bg-card p-5">
          <div className="flex flex-col md:flex-row gap-3 mb-5">
            <div className="relative flex-1">
              <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" />
              <input
                type="text"
                placeholder="Search data..."
                value={search}
                onChange={e => setSearch(e.target.value)}
                className="w-full bg-secondary border border-border rounded-lg pl-9 pr-3 py-2 text-sm text-foreground placeholder-muted-foreground focus:outline-none focus:ring-1 focus:ring-primary"
              />
            </div>
            <div className="flex items-center gap-2">
              <Calendar size={14} className="text-muted-foreground" />
              <input
                type="date"
                value={startDate}
                onChange={e => setStartDate(e.target.value)}
                className="bg-secondary border border-border rounded-lg px-3 py-2 text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-primary"
              />
              <span className="text-muted-foreground text-xs">to</span>
              <input
                type="date"
                value={endDate}
                onChange={e => setEndDate(e.target.value)}
                className="bg-secondary border border-border rounded-lg px-3 py-2 text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-primary"
              />
            </div>
          </div>

          <div className="overflow-x-auto scrollbar-thin">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-border">
                  {columns.map(col => (
                    <th key={col} className="py-2 px-3 text-left text-muted-foreground font-medium uppercase tracking-wider whitespace-nowrap">
                      {col === 'timestamp' ? 'Time' : col === 'aucausticRMS' ? 'Acoustic' : col}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rows.length === 0 && (
                  <tr>
                    <td colSpan={columns.length} className="py-8 text-center text-muted-foreground">
                      {loadError ? 'No data — backend unreachable' : loading ? 'Loading…' : 'No records match the filters'}
                    </td>
                  </tr>
                )}
                {rows.map(row => (
                  <tr key={row.timestamp} className="border-b border-border/50 hover:bg-secondary/30 transition-colors">
                    {columns.map(col => (
                      <td key={col} className="py-2 px-3 font-mono whitespace-nowrap text-foreground">
                        {col === 'timestamp' ? formatDateTime(row[col]) : Number(row[col] ?? 0).toFixed(3)}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="flex items-center justify-between mt-4">
            <span className="text-xs text-muted-foreground">
              Showing {total === 0 ? 0 : (page - 1) * ITEMS_PER_PAGE + 1}–{Math.min(page * ITEMS_PER_PAGE, total)} of {total.toLocaleString('en-US')}
            </span>
            <div className="flex items-center gap-2">
              <button
                onClick={() => setPage(p => Math.max(1, p - 1))}
                disabled={page === 1}
                className="p-1.5 rounded-lg bg-secondary text-foreground disabled:opacity-30 hover:bg-secondary/80 transition-colors"
              >
                <ChevronLeft size={14} />
              </button>
              <span className="text-xs text-muted-foreground font-mono">{page}/{totalPages || 1}</span>
              <button
                onClick={() => setPage(p => Math.min(totalPages, p + 1))}
                disabled={page === totalPages || totalPages === 0}
                className="p-1.5 rounded-lg bg-secondary text-foreground disabled:opacity-30 hover:bg-secondary/80 transition-colors"
              >
                <ChevronRight size={14} />
              </button>
            </div>
          </div>
        </div>
      </main>
    </div>
  );
};

export default History;
