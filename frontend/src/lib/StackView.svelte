<script>
  import ActionBar from "./ActionBar.svelte";
  import JobPanel from "./JobPanel.svelte";
  import LogViewer from "./LogViewer.svelte";
  import TerminalPanel from "./TerminalPanel.svelte";
  import { STATUS_LABEL, ago } from "./format.js";

  let { detail, onExpired, onBack, onChanged } = $props();

  // The action being shown: one started here, or one already running when the
  // page was opened (the server reports it in detail.job).
  let jobId = $state(null);
  let jobRunning = $state(false);
  $effect(() => {
    if (detail.job && !jobId) {
      jobId = detail.job;
      jobRunning = true;
    }
  });

  // The container a terminal is open for (one per stack view).
  let terminalFor = $state(null);

  function started(id) {
    jobId = id;
    jobRunning = true;
  }

  function finished() {
    jobRunning = false;
    onChanged();
  }

  // A new log stream whenever the running containers change: an update replaces
  // them, stop and start change which ones run, and a restart (by the user or by
  // Docker's restart policy) keeps the ids but changes when they started.
  let streamKey = $derived(
    detail.name +
      "|" +
      detail.containers
        .filter((c) => c.state === "running")
        .map((c) => `${c.id}@${c.started_at}`)
        .sort()
        .join(","),
  );

  // Re-render "up 5m" style times every 30 s without asking the server.
  let now = $state(Date.now());
  $effect(() => {
    const timer = setInterval(() => (now = Date.now()), 30000);
    return () => clearInterval(timer);
  });

  let services = $derived([
    ...new Set([...detail.services, ...detail.containers.map((c) => c.service).filter(Boolean)]),
  ]);

  function since(c) {
    if (c.state === "running") return `up ${ago(c.started_at, now)}`;
    if (c.finished_at && !c.finished_at.startsWith("0001-")) {
      return `exited ${c.exit_code ?? ""} · ${ago(c.finished_at, now)} ago`;
    }
    return c.state;
  }
</script>

<article class="stack-view">
  <header class="stack-head">
    <button type="button" class="back secondary small" onclick={onBack}>← Stacks</button>
    <h1>{detail.name}</h1>
    <span class="badge {detail.status}">{STATUS_LABEL[detail.status]}</span>
    {#if detail.unhealthy}<span class="badge bad">Unhealthy</span>{/if}
    <span class="muted file">{detail.compose_file}</span>
  </header>

  <ActionBar {detail} busy={jobRunning || !!detail.job} onStarted={started} {onExpired} />
  {#if jobId}
    {#key jobId}
      <JobPanel {jobId} onFinished={finished} onDismiss={() => (jobId = null)} {onExpired} />
    {/key}
  {/if}

  {#if detail.self}
    <p class="notice info">
      This is the stack this app runs in. It stays read-only here: change it over SSH.
    </p>
  {/if}
  {#if detail.error}<p class="notice error">{detail.error}</p>{/if}
  {#if detail.missing_services.length}
    <p class="notice info">Not created yet: {detail.missing_services.join(", ")}</p>
  {/if}
  {#if detail.buildable.length}
    <p class="notice info">
      Built locally, so skipped by Update: {detail.buildable.join(", ")}
    </p>
  {/if}

  <section class="containers" aria-label="Containers">
    <div class="c-row c-head" aria-hidden="true">
      <span>Service</span><span>State</span><span>Image</span><span>Ports</span><span>Restarts</span><span></span>
    </div>
    {#each detail.containers as c (c.id)}
      <div class="c-row">
        <span class="c-service">
          <span class="dot {c.state === 'running' ? 'running' : 'stopped'}" class:unhealthy={c.health === "unhealthy"} aria-hidden="true"></span>
          <strong>{c.service || c.name}</strong>
          <span class="muted c-name">{c.name}</span>
        </span>
        <span class="c-state">
          {since(c)}
          {#if c.health}<span class="health {c.health}">{c.health}</span>{/if}
          {#if c.oom_killed}<span class="health unhealthy">out of memory</span>{/if}
        </span>
        <span class="c-image" title={c.image}>{c.image}</span>
        <span class="c-ports">
          {#each c.ports as port (port)}<span class="port">{port}</span>{:else}<span class="muted none">–</span>{/each}
        </span>
        <span class="c-restarts"><span class="label">Restarts</span>{c.restart_count}</span>
        <span class="c-tools">
          {#if c.state === "running" && !detail.self}
            <button
              type="button"
              class="secondary small"
              aria-pressed={terminalFor?.id === c.id}
              onclick={() => (terminalFor = { id: c.id, name: c.name, service: c.service })}
            >Terminal</button>
          {/if}
        </span>
      </div>
    {:else}
      <p class="muted">No containers. This stack has not been started.</p>
    {/each}
  </section>

  {#if terminalFor}
    {#key terminalFor.id}
      <TerminalPanel stack={detail.name} container={terminalFor} onClose={() => (terminalFor = null)} {onExpired} />
    {/key}
  {/if}

  {#if detail.containers.length}
    {#key streamKey}
      <LogViewer stack={detail.name} {services} {onExpired} />
    {/key}
  {/if}
</article>
