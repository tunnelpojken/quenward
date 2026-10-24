import { mount } from "svelte";
// Bundled monospace font for logs, the terminal and the editor, so text looks the
// same on every system. Files are served from this app (the CSP allows only that).
import "@fontsource/jetbrains-mono/latin-400.css";
import "@fontsource/jetbrains-mono/latin-700.css";
// The MAGI theme's condensed console font. Browsers download a font only when a
// page uses it, so the neutral theme never loads it.
import "@fontsource/barlow-condensed/latin-500.css";
import "@fontsource/barlow-condensed/latin-700.css";
import "./app.css";
import "./magi.css";
import App from "./App.svelte";
import { applyTheme, storedTheme } from "./lib/theme.js";

// Before the first render, so a MAGI page never flashes in the neutral colours.
applyTheme(storedTheme());

const app = mount(App, { target: document.getElementById("app") });

export default app;
