<script>
  import { tick, untrack } from "svelte";
  import { openLogs } from "./api.js";
  import { clock, sourceClass } from "./format.js";

  let { stack, services, onExpired } = $props();

  const MAX_LINES = 2000;

  let lines = $state.raw([]);
  let container = $state("");
  let filter = $state("");
  let follow = $state(true);
  let wrap = $state(true);
  let status = $state("connecting"); // connecting | live | reconnecting | closed
  let problem = $state("");
  let dropped = $state(0);
  let box = $state(null);

  let counter = 0;
  let shown = $derived.by(() => {
    const f = filter.trim().toLowerCase();
    if (!f) return lines;
    return lines.filter((l) => l.m.toLowerCase().includes(f) || l.s.toLowerCase().includes(f));
  });

  // The stack prop comes from a parent object that is replaced on every refresh.
  // A derived only notifies when the name itself changes, so the stream is not
  // reopened every few seconds.
  const streamName = $derived(stack);

  // (Re)connect whenever the stack or the selected container changes.
  $effect(() => {
    const name = streamName;
    const only = container;
    let ws = null;
    let retry = null;
    let attempts = 0;
    let stopped = false;

    function connect() {
      lines = [];
      dropped = 0;
      problem = "";
      status = attempts ? "reconnecting" : "connecting";
      ws = openLogs(name, { tail: 200, container: only || null });
      ws.onmessage = (event) => {
        let data;
        try {
          data = JSON.parse(event.data);
        } catch {
          return;
        }
        if (data.type === "lines") {
          const added = data.lines.map((l) => ({ ...l, k: counter++ }));
          const next = lines.concat(added);
          lines = next.length > MAX_LINES ? next.slice(next.length - MAX_LINES) : next;
        } else if (data.type === "live") {
          status = "live";
          attempts = 0;
        } else if (data.type === "dropped") {
          dropped = data.count;
        } else if (data.type === "ended") {
          status = "ended";
        }
      };
      ws.onclose = (event) => {
        if (stopped) return;
        if (event.code === 4401) {
          status = "closed";
          onExpired();
          return;
        }
        if (event.code === 4410) {
          // Every container stopped or was replaced. No retry: a new stream opens
          // by itself once the stack's running containers change.
          status = "ended";
          return;
        }
        if (event.code === 4404) {
          status = "closed";
          problem = "This stack or container no longer exists.";
          return;
        }
        if (event.code === 1013) problem = "Too many log views are open. Close another tab.";
        else if (event.code === 1011) problem = event.reason || "Docker is not reachable.";
        // Anything else (server restart, network blip): try again, slower each time.
        attempts += 1;
        status = "reconnecting";
        retry = setTimeout(connect, Math.min(30000, 1000 * 2 ** Math.min(attempts, 5)));
      };
    }

    untrack(connect);
    return () => {
      stopped = true;
      clearTimeout(retry);
      ws?.close();
    };
  });

  // Keep the newest line in view while following.
  $effect(() => {
    shown;
    if (follow && box) tick().then(() => box && (box.scrollTop = box.scrollHeight));
  });

  function onScroll() {
    if (!box) return;
    follow = box.scrollTop + box.clientHeight >= box.scrollHeight - 8;
  }

  function jumpToEnd() {
    follow = true;
  }
</script>

<section class="logs" aria-label="Logs">
  <div class="logs-bar">
    <h2>Logs</h2>
    <span class="live-state {status}">{status === "live" ? "Live" : status === "ended" ? "Not running · recent output" : status === "closed" ? "Stopped" : status === "reconnecting" ? "Reconnecting…" : "Connecting…"}</span>
    <div class="logs-controls">
      <select bind:value={container} aria-label="Container">
        <option value="">All services</option>
        {#each services as service (service)}
          <option value={service}>{service}</option>
        {/each}
      </select>
      <input type="search" placeholder="Filter lines" aria-label="Filter lines" bind:value={filter} />
      <label class="check"><input type="checkbox" bind:checked={wrap} /> Wrap</label>
      <button type="button" class="secondary small" onclick={() => (lines = [])}>Clear</button>
    </div>
  </div>
  {#if problem}<p class="notice error">{problem}</p>{/if}
  {#if dropped}
    <p class="notice info">{dropped} lines were skipped because the output was too fast to show.</p>
  {/if}
  <div class="log-box" class:wrap bind:this={box} onscroll={onScroll} tabindex="0" role="log" aria-live="off">
    {#each shown as line (line.k)}
      <div class="ln"><span class="ts">{clock(line.t)}</span><span class="src {sourceClass(line.s, services)}">{line.s}</span><span class="msg">{line.m}</span></div>
    {:else}
      <p class="muted log-empty">{status === "live" || status === "ended" ? (filter ? "No line matches the filter." : "No log output yet.") : ""}</p>
    {/each}
  </div>
  {#if !follow}
    <button type="button" class="jump small" onclick={jumpToEnd}>Jump to newest ↓</button>
  {/if}
</section>
