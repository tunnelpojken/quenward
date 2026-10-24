export const STATUS_LABEL = {
  running: "Running",
  partial: "Partly running",
  stopped: "Stopped",
  not_deployed: "Not deployed",
};

function parse(iso) {
  if (!iso || iso.startsWith("0001-")) return null;
  // Docker gives nanoseconds; JavaScript dates understand milliseconds.
  const date = new Date(iso.replace(/(\.\d{3})\d+/, "$1"));
  return Number.isNaN(date.getTime()) ? null : date;
}

export function ago(iso, now = Date.now()) {
  const date = parse(iso);
  if (!date) return "";
  const s = Math.max(0, Math.round((now - date.getTime()) / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}h ${m % 60}m`;
  return `${Math.floor(h / 24)}d ${h % 24}h`;
}

export function clock(iso) {
  const date = parse(iso);
  if (!date) return "";
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

// Same service -> same colour class, across reloads. The stack's services get
// distinct colours by their sorted position (the first six never share one); a
// name not in the list falls back to a hash of the name.
export function sourceClass(name, services = []) {
  const index = [...services].sort().indexOf(name);
  if (index !== -1) return `src-${index % 6}`;
  let hash = 0;
  for (const ch of name) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
  return `src-${hash % 6}`;
}
