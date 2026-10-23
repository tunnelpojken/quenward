<script>
  import Notice from "../lib/Notice.svelte";
  import StackList from "../lib/StackList.svelte";
  import StackView from "../lib/StackView.svelte";
  import { getStack, getStacks, logout } from "../lib/api.js";

  let { session, onLoggedOut, onExpired } = $props();

  const LIST_EVERY = 10000;
  const DETAIL_EVERY = 5000;

  let stacks = $state([]);
  let warning = $state("");
  let elsewhere = $state([]);
  let dockerError = $state("");
  let loaded = $state(false);
  let selected = $state(stackFromHash());
  let detail = $state(null);
  let detailError = $state("");
  let busy = $state(false);

  let summary = $derived.by(() => {
    const count = (status) => stacks.filter((s) => s.status === status).length;
    const parts = [
      [count("running"), "running"],
      [count("partial"), "partly running"],
      [count("stopped"), "stopped"],
      [count("not_deployed"), "not deployed"],
    ].filter(([n]) => n > 0);
    return parts.map(([n, label]) => `${n} ${label}`).join(" · ");
  });
  let unhealthy = $derived(stacks.filter((s) => s.unhealthy).length);

  function stackFromHash() {
    const m = location.hash.match(/^#\/stack\/(.+)$/);
    return m ? decodeURIComponent(m[1]) : null;
  }

  function select(name) {
    location.hash = name ? `#/stack/${encodeURIComponent(name)}` : "";
  }

  function handle(error) {
    if (error.status === 401) {
      onExpired();
      return true;
    }
    return false;
  }

  async function loadList(background) {
    try {
      const data = await getStacks({ background });
      stacks = data.stacks;
      warning = data.warning || "";
      elsewhere = data.started_elsewhere || [];
      dockerError = "";
    } catch (e) {
      if (!handle(e)) dockerError = e.message;
    } finally {
      loaded = true;
    }
  }

  async function loadDetail(name, background) {
    if (!name) {
      detail = null;
      return;
    }
    try {
      const data = await getStack(name, { background });
      if (name === selected) {
        detail = data;
        detailError = "";
      }
    } catch (e) {
      if (handle(e)) return;
      if (name === selected) {
        detail = null;
        detailError = e.status === 404 ? `There is no stack called "${name}".` : e.message;
      }
    }
  }

  // Refresh on a timer while the tab is visible. These count as background
  // requests, so they do not extend the session.
  $effect(() => {
    loadList(false);
    const timer = setInterval(() => document.visibilityState === "visible" && loadList(true), LIST_EVERY);
    return () => clearInterval(timer);
  });

  $effect(() => {
    const name = selected;
    detail = null;
    detailError = "";
    loadDetail(name, false);
    const timer = setInterval(
      () => document.visibilityState === "visible" && loadDetail(name, true),
      DETAIL_EVERY,
    );
    return () => clearInterval(timer);
  });

  async function doLogout() {
    busy = true;
    try {
      await logout();
    } catch {
      // Logged out locally either way.
    }
    onLoggedOut();
  }
</script>

<svelte:window onhashchange={() => location.hash.startsWith("#/reset/") || (selected = stackFromHash())} />

<div class="app-shell">
  <header class="topbar">
    <span class="brand-mark">docker-project</span>
    <span class="topbar-spacer"></span>
    <span class="muted user">{session.username}</span>
    <button type="button" class="secondary small" onclick={doLogout} disabled={busy}>Log out</button>
  </header>

  {#if dockerError}<Notice message={dockerError} />{/if}
  {#if warning}<Notice message={warning} />{/if}
  {#if elsewhere.length}
    <p class="notice info banner">
      Containers for {elsewhere.join(", ")} were started from another folder than the one shown
      here. Mount the stacks folder at the same path as on the host.
    </p>
  {/if}

  <div class="layout" class:has-selection={selected}>
    <aside class="sidebar">
      {#if loaded}
        <StackList {stacks} {selected} onSelect={select} />
      {:else}
        <p class="muted pad">Loading stacks…</p>
      {/if}
    </aside>
    <main class="main">
      {#if detail}
        <StackView {detail} {onExpired} onBack={() => select(null)} />
      {:else if detailError}
        <div class="pad"><Notice message={detailError} /></div>
      {:else if selected}
        <p class="muted pad">Loading {selected}…</p>
      {:else}
        <div class="placeholder">
          {#if stacks.length}
            <p class="summary">{stacks.length} {stacks.length === 1 ? "stack" : "stacks"}: {summary}</p>
            {#if unhealthy}<p class="notice error">{unhealthy} with an unhealthy container</p>{/if}
          {/if}
          <p class="muted">Select a stack to see its containers and logs.</p>
        </div>
      {/if}
    </main>
  </div>
</div>
