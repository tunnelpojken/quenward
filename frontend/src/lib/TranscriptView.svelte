<script>
  import "@xterm/xterm/css/xterm.css";
  import Notice from "./Notice.svelte";
  import { getTranscript } from "./api.js";
  import { FONT, adoptStyles, base64ToBytes, loadXterm, themeFromCss } from "./xterm.js";

  let { id, onExpired } = $props();

  let box = $state(null);
  let error = $state("");
  let keystrokes = $state("");
  let truncated = $state(false);
  let loading = $state(true);

  // Typed input made readable: Enter, Backspace, arrows and Ctrl+key become visible.
  const KEYS = {
    "\r": "⏎\n",
    "\n": "⏎\n",
    "\x7f": "⌫",
    "\t": "⇥",
    "\x1b[A": "↑",
    "\x1b[B": "↓",
    "\x1b[C": "→",
    "\x1b[D": "←",
    "\x1b": "[Esc]", // not "⎋": the bundled font has no such symbol
  };

  // Answers the terminal sends by itself when the shell asks (cursor position,
  // terminal type). Recorded, but not something the user typed.
  const AUTO_REPLIES = /\x1b\[\??[\d;]*[Rc]/g;

  function readable(text) {
    text = text.replace(AUTO_REPLIES, "");
    let out = "";
    for (let i = 0; i < text.length; ) {
      const arrow = text.slice(i, i + 3);
      if (KEYS[arrow]) {
        out += KEYS[arrow];
        i += 3;
        continue;
      }
      const ch = text[i++];
      const code = ch.charCodeAt(0);
      if (KEYS[ch]) out += KEYS[ch];
      else if (code < 32) out += `^${String.fromCharCode(code + 64)}`;
      else out += ch;
    }
    return out;
  }

  $effect(() => {
    if (!box) return;
    let term = null;
    let release = null;
    let closed = false;

    (async () => {
      let data, Terminal, FitAddon;
      try {
        [data, { Terminal, FitAddon }] = await Promise.all([getTranscript(id), loadXterm()]);
      } catch (e) {
        loading = false;
        if (e.status === 401) onExpired();
        else error = e.message;
        return;
      }
      if (closed) return;
      release = adoptStyles(box);
      term = new Terminal({
        fontFamily: FONT,
        fontSize: 13,
        scrollback: 20000,
        disableStdin: true,
        cursorBlink: false,
        theme: themeFromCss(),
      });
      const fit = new FitAddon();
      term.loadAddon(fit);
      term.open(box);
      fit.fit();
      const decoder = new TextDecoder();
      let typed = "";
      for (const [, kind, payload] of data.events) {
        const bytes = base64ToBytes(payload);
        if (kind === "o") term.write(bytes);
        else typed += decoder.decode(bytes, { stream: true });
      }
      keystrokes = readable(typed);
      truncated = data.session.truncated;
      loading = false;
    })();

    return () => {
      closed = true;
      term?.dispose();
      release?.();
    };
  });
</script>

{#if error}<Notice message={error} />{/if}
{#if truncated}
  <p class="notice info">This session was longer than the recording limit; the end was not recorded.</p>
{/if}
{#if loading}<p class="muted">Decrypting…</p>{/if}
<div class="term-box replay" class:hidden={!!error} bind:this={box}></div>

{#if keystrokes}
  <details class="keystrokes">
    <summary>Keystrokes</summary>
    <p class="muted">
      Everything typed, including what the screen did not show (such as passwords at a prompt).
    </p>
    <pre>{keystrokes}</pre>
  </details>
{/if}
