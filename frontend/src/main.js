import { mount } from "svelte";
// Bundled monospace font for logs, the terminal and the editor, so text looks the
// same on every system. Files are served from this app (the CSP allows only that).
import "@fontsource/jetbrains-mono/latin-400.css";
import "@fontsource/jetbrains-mono/latin-700.css";
import "./app.css";
import App from "./App.svelte";

const app = mount(App, { target: document.getElementById("app") });

export default app;
