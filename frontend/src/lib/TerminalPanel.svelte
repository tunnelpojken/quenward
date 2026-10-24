<script>
  import "@xterm/xterm/css/xterm.css";
  import { openTerminal } from "./api.js";
  import { FONT, adoptStyles, loadXterm, themeFromCss } from "./xterm.js";
  import { onThemeChange } from "./theme.js";

  let { stack, container, onClose, onExpired } = $props();

  let box = $state(null);
  let status = $state("connecting"); // connecting | open | ended
  let shell = $state("");
  let recorded = $state(false);
  let message = $state("");
  let attempt = $state(0); // bump to reconnect: a new shell, a new session

  const CLOSE_REASONS = {
    1009: "The text sent at once was too large.",
    1013: "Too many terminals are open. Close one first.",
    4403: "This app's own stack is read-only.",
    4404: "This container no longer exists.",
    4409: "The container is not running.",
  };

  $effect(() => {
    attempt;
    if (!box) return;
    let ws = null;
    let term = null;
    let fit = null;
    let resizeObserver = null;
    let release = null;
    let closed = false;
    let idleMinutes = 15;

    status = "connecting";
    message = "";

    (async () => {
      const { Terminal, FitAddon } = await loadXterm();
      if (closed) return;
      release = adoptStyles(box);
      term = new Terminal({
        fontFamily: FONT,
        fontSize: 13,
        cursorBlink: true,
        scrollback: 5000,
        theme: themeFromCss(),
      });
      fit = new FitAddon();
      term.loadAddon(fit);
      term.open(box);
      fit.fit();

      ws = openTerminal(stack, container.id, term.rows, term.cols);
      ws.binaryType = "arraybuffer";
      const send = (event) => ws.readyState === WebSocket.OPEN && ws.send(JSON.stringify(event));

      ws.onmessage = (event) => {
        if (typeof event.data !== "string") {
          term.write(new Uint8Array(event.data));
          return;
        }
        let data;
        try {
          data = JSON.parse(event.data);
        } catch {
          return;
        }
        if (data.type === "ready") {
          status = "open";
          shell = data.shell;
          recorded = data.recorded;
          idleMinutes = data.idle_minutes;
          term.focus();
        } else if (data.type === "exit") {
          message = `The shell exited${data.code === null ? "" : ` with code ${data.code}`}.`;
        }
      };
      ws.onclose = (event) => {
        if (closed) return;
        status = "ended";
        term.options.cursorBlink = false;
        if (event.code === 4401) {
          onExpired();
          return;
        }
        if (event.code === 4410) message ||= "The shell exited.";
        else if (event.code === 4408)
          message = `Closed after ${idleMinutes} minutes without typing.`;
        else message = CLOSE_REASONS[event.code] || event.reason || "The connection was lost.";
      };

      term.onData((data) => send({ type: "input", data }));
      term.onResize(({ rows, cols }) => send({ type: "resize", rows, cols }));
      resizeObserver = new ResizeObserver(() => fit.fit());
      resizeObserver.observe(box);
    })();

    // Follow a theme switch (or the system's light/dark) while the terminal is open.
    const stopTheme = onThemeChange(() => {
      if (term) term.options.theme = themeFromCss();
    });

    return () => {
      closed = true;
      stopTheme();
      resizeObserver?.disconnect();
      ws?.close();
      term?.dispose();
      release?.();
    };
  });
</script>

<section class="terminal" aria-label="Terminal">
  <div class="logs-bar">
    <h2>Terminal</h2>
    <span class="muted">{container.service || container.name}</span>
    <span class="live-state" class:live={status === "open"}>
      {status === "connecting" ? "Connecting…" : status === "open" ? `Connected · ${shell}` : "Ended"}
    </span>
    {#if status === "open"}
      <span class="term-recorded" class:on={recorded}>
        {recorded ? "Recorded (encrypted)" : "Not recorded"}
      </span>
    {/if}
    <div class="logs-controls">
      {#if status === "ended"}
        <button type="button" class="secondary small" onclick={() => attempt++}>New session</button>
      {/if}
      <button type="button" class="secondary small" onclick={onClose}>Close</button>
    </div>
  </div>
  {#if message}<p class="notice info">{message}</p>{/if}
  <div class="term-box" bind:this={box}></div>
</section>
