<script>
  import Notice from "../lib/Notice.svelte";
  import TranscriptView from "../lib/TranscriptView.svelte";
  import { getTerminalSessions } from "../lib/api.js";

  let { sessionId, onExpired } = $props();

  let data = $state(null);
  let error = $state("");

  $effect(() => {
    getTerminalSessions()
      .then((d) => (data = d))
      .catch((e) => (e.status === 401 ? onExpired() : (error = e.message)));
  });

  let current = $derived(data?.sessions.find((s) => s.id === sessionId) ?? null);

  function when(iso) {
    return iso ? new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "";
  }

  function duration(s) {
    if (!s.ended_at) return "open";
    const secs = Math.round((new Date(s.ended_at) - new Date(s.started_at)) / 1000);
    return secs < 60 ? `${secs}s` : `${Math.floor(secs / 60)}m ${secs % 60}s`;
  }

  function ending(s) {
    if (!s.ended_at) return "Still open";
    const reasons = {
      exited: "Shell exited",
      closed: "Closed",
      idle: "Idle timeout",
      "session expired": "Session expired",
      "app restarted": "App restarted",
      "input too large": "Input too large",
    };
    // Only a shell that exited by itself has a meaningful exit code; when the app
    // ends it (closing, idle), the code is just the hangup signal it sent.
    const code = s.end_reason === "exited" && s.exit_code !== null ? ` (${s.exit_code})` : "";
    return (reasons[s.end_reason] || s.end_reason) + code;
  }

  function size(bytes) {
    return bytes < 1024 ? `${bytes} B` : `${Math.round(bytes / 1024)} kB`;
  }
</script>

<section class="history">
  {#if sessionId}
    <a class="back-link" href="#/terminals">← Terminal history</a>
    {#if current}
      <h1>{current.stack} · {current.service || current.container_name}</h1>
      <p class="muted">{when(current.started_at)} · {current.shell} · {duration(current)} · {ending(current)}</p>
      <TranscriptView id={sessionId} {onExpired} />
    {:else if data}
      <Notice message="This session no longer exists. History is kept for {data.retention_days} days." />
    {/if}
  {:else}
    <h1>Terminal history</h1>
    {#if data}
      <p class="muted">
        The last {data.retention_days} days.
        {#if data.recording}
          Sessions are recorded, encrypted, and deleted after that.
        {:else}
          Transcripts are off: set <code>TRANSCRIPT_KEY_FILE</code> to record what is typed and shown.
        {/if}
      </p>
    {/if}
  {/if}

  {#if error}<Notice message={error} />{/if}

  {#if data && !sessionId}
    {#if data.sessions.length}
      <div class="h-table">
        <div class="h-row h-head" aria-hidden="true">
          <span>Started</span><span>Container</span><span>Length</span><span>Ended</span><span>Transcript</span>
        </div>
        {#each data.sessions as s (s.id)}
          <div class="h-row">
            <span>{when(s.started_at)}</span>
            <span><strong>{s.stack}</strong> · {s.service || s.container_name} <span class="muted">({s.shell})</span></span>
            <span>{duration(s)}</span>
            <span>{ending(s)}</span>
            <span>
              {#if s.recorded}
                <a href="#/terminals/{s.id}">View</a>
                <span class="muted">{size(s.transcript_bytes)}{s.truncated ? ", cut off" : ""}</span>
              {:else}
                <span class="muted">Not recorded</span>
              {/if}
            </span>
          </div>
        {/each}
      </div>
    {:else}
      <p class="muted">No terminal sessions yet.</p>
    {/if}
  {/if}
</section>
