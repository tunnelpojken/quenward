<script>
  import { tick } from "svelte";
  import { getJob } from "./api.js";

  let { jobId, onFinished, onDismiss, onExpired } = $props();

  const LABEL = { start: "Starting", stop: "Stopping", restart: "Restarting", update: "Updating" };
  const DONE = { start: "Started", stop: "Stopped", restart: "Restarted", update: "Updated" };

  let job = $state(null);
  let lines = $state.raw([]);
  let error = $state("");
  let box = $state(null);

  // Follow the job's output until it finishes. Polling counts as background
  // activity, so watching an action does not extend the session.
  $effect(() => {
    const id = jobId;
    let next = 0;
    let timer = null;
    let stopped = false;

    async function poll() {
      try {
        const data = await getJob(id, next);
        if (stopped) return;
        job = data;
        if (data.lines.length) lines = lines.concat(data.lines);
        next = data.next;
        if (data.status === "running") timer = setTimeout(poll, 600);
        else onFinished(data);
      } catch (e) {
        if (stopped) return;
        if (e.status === 401) onExpired();
        else if (e.status === 404) error = "This action is no longer available.";
        else timer = setTimeout(poll, 2000);
      }
    }

    lines = [];
    poll();
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  });

  $effect(() => {
    lines;
    if (box) tick().then(() => box && (box.scrollTop = box.scrollHeight));
  });
</script>

<section class="job" class:ok={job?.status === "ok"} class:failed={job?.status === "error"} aria-live="polite">
  <div class="job-head">
    {#if !job || job.status === "running"}
      <span class="spinner" aria-hidden="true"></span>
      <strong>{job ? LABEL[job.action] : "Starting action"}…</strong>
    {:else if job.status === "ok"}
      <strong>{DONE[job.action]}</strong>
    {:else}
      <strong>{LABEL[job.action]} failed</strong>
    {/if}
    {#if job && job.status !== "running"}
      <button type="button" class="secondary small" onclick={onDismiss}>Dismiss</button>
    {/if}
  </div>
  {#if error}<p class="notice error">{error}</p>{/if}
  {#if job?.summary.length}
    <ul class="job-summary">
      {#each job.summary as item (item)}<li>{item}</li>{/each}
    </ul>
  {/if}
  <pre class="job-output" bind:this={box}>{lines.join("\n")}</pre>
</section>
