// xterm.js, loaded only when a terminal is opened (it is the largest dependency).
//
// The Content-Security-Policy forbids <style> elements ("style-src 'self'"), and
// xterm.js creates three for its colours and cell sizes. Rather than weakening the
// policy for the whole app, adoptStyles() copies the CSS of those elements, and
// only those inside the terminal's own box, into constructable stylesheets, which
// the policy does not block. The elements are then detached: xterm keeps
// updating them, and a detached element is never checked or applied.

// The bundled font (see main.js): xterm.js lays text out on a fixed cell grid, and
// some systems' default monospace font (e.g. Nimbus Mono PS) leaves gaps around
// letters like "f" on that grid.
export const FONT = '"JetBrains Mono", ui-monospace, "DejaVu Sans Mono", Menlo, Consolas, monospace';

export async function loadXterm() {
  const [{ Terminal }, { FitAddon }] = await Promise.all([
    import("@xterm/xterm"),
    import("@xterm/addon-fit"),
    // xterm measures the font when it opens, so it must be loaded first.
    document.fonts.load('13px "JetBrains Mono"').catch(() => {}),
    document.fonts.load('bold 13px "JetBrains Mono"').catch(() => {}),
  ]);
  return { Terminal, FitAddon };
}

export function adoptStyles(host) {
  const sheets = new Map(); // style element -> stylesheet
  const watcher = new MutationObserver(() => {
    for (const [el, sheet] of sheets) sheet.replaceSync(el.textContent || "");
  });

  function take(el) {
    if (sheets.has(el)) return;
    const sheet = new CSSStyleSheet();
    sheet.replaceSync(el.textContent || "");
    sheets.set(el, sheet);
    document.adoptedStyleSheets = [...document.adoptedStyleSheets, sheet];
    watcher.observe(el, { childList: true, characterData: true, subtree: true });
    el.remove();
  }

  const finder = new MutationObserver(() => host.querySelectorAll("style").forEach(take));
  finder.observe(host, { childList: true, subtree: true });

  return () => {
    finder.disconnect();
    watcher.disconnect();
    const ours = new Set(sheets.values());
    document.adoptedStyleSheets = document.adoptedStyleSheets.filter((s) => !ours.has(s));
    sheets.clear();
  };
}

// Colours from the app's own theme, so the terminal follows light and dark mode.
export function themeFromCss() {
  const css = getComputedStyle(document.documentElement);
  const v = (name) => css.getPropertyValue(name).trim();
  return {
    background: v("--log-bg"),
    foreground: v("--text"),
    cursor: v("--accent"),
    cursorAccent: v("--log-bg"),
    selectionBackground: v("--active"),
  };
}

export function base64ToBytes(text) {
  const raw = atob(text);
  const bytes = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
  return bytes;
}
