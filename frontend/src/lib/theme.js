// The theme switch. "neutral" follows the system's light or dark mode; "magi" is
// a dark NERV-console look. The choice is remembered per browser (it is a
// display preference, not an account setting) and applied as <html data-theme>,
// which app.css and magi.css style from.

export const THEMES = [
  { id: "neutral", label: "Neutral" },
  { id: "magi", label: "MAGI" },
];

const KEY = "quenward.theme";
const listeners = new Set();

function valid(id) {
  return THEMES.some((t) => t.id === id);
}

export function storedTheme() {
  try {
    const id = localStorage.getItem(KEY);
    return valid(id) ? id : "neutral";
  } catch {
    return "neutral"; // storage blocked (private window, settings): default theme
  }
}

export function currentTheme() {
  return document.documentElement.dataset.theme || "neutral";
}

export function applyTheme(id) {
  if (!valid(id)) id = "neutral";
  document.documentElement.dataset.theme = id;
  for (const fn of listeners) fn(id);
}

export function setTheme(id) {
  applyTheme(id);
  try {
    localStorage.setItem(KEY, id);
  } catch {
    // Not remembered, but still applied for this visit.
  }
}

// Called with the theme id whenever it changes, and also when the system switches
// between light and dark (the neutral theme follows it). Returns an unsubscribe.
export function onThemeChange(fn) {
  listeners.add(fn);
  const media = window.matchMedia("(prefers-color-scheme: dark)");
  const onMedia = () => fn(currentTheme());
  media.addEventListener("change", onMedia);
  return () => {
    listeners.delete(fn);
    media.removeEventListener("change", onMedia);
  };
}
