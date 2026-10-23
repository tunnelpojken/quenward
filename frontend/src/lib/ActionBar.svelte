<script>
  import { runAction } from "./api.js";

  let { detail, busy, onStarted, onExpired } = $props();

  // Disruptive actions are confirmed first: stopping e.g. a DNS stack takes a
  // service away for the whole network.
  const CONFIRM = {
    stop: "The services in this stack stop until you start them again.",
    restart: "The services in this stack are briefly unavailable.",
    update: "New images are pulled and changed containers are recreated. The previous image of each service is kept.",
  };
  const LABEL = { start: "Start", stop: "Stop", restart: "Restart", update: "Update" };

  let confirming = $state(null);
  let error = $state("");
  let sending = $state(false);

  let running = $derived(detail.running);
  let total = $derived(detail.total);
  let actions = $derived(
    [
      detail.status !== "running" && "start",
      running > 0 && "restart",
      running > 0 && "stop",
      "update",
    ].filter(Boolean),
  );

  async function run(action) {
    confirming = null;
    error = "";
    sending = true;
    try {
      const job = await runAction(detail.name, action);
      onStarted(job.id);
    } catch (e) {
      if (e.status === 401) onExpired();
      else if (e.status === 409 && e.detail?.job) onStarted(e.detail.job);
      else error = e.message;
    } finally {
      sending = false;
    }
  }

  function choose(action) {
    error = "";
    if (CONFIRM[action]) confirming = action;
    else run(action);
  }
</script>

{#if !detail.self}
  <div class="actions">
    {#each actions as action (action)}
      <button
        type="button"
        class="small"
        class:secondary={action !== "start"}
        class:danger={action === "stop"}
        disabled={busy || sending}
        onclick={() => choose(action)}
      >
        {LABEL[action]}
      </button>
    {/each}
  </div>
  {#if confirming}
    <div class="confirm" role="alertdialog" aria-label="Confirm {LABEL[confirming].toLowerCase()}">
      <p>
        <strong>{LABEL[confirming]} {detail.name}{total ? ` (${total} ${total === 1 ? "container" : "containers"})` : ""}?</strong>
        {CONFIRM[confirming]}
      </p>
      <div class="confirm-buttons">
        <button type="button" class="small" class:danger={confirming === "stop"} onclick={() => run(confirming)}>
          {LABEL[confirming]}
        </button>
        <button type="button" class="secondary small" onclick={() => (confirming = null)}>Cancel</button>
      </div>
    </div>
  {/if}
  {#if error}<p class="notice error">{error}</p>{/if}
{/if}
