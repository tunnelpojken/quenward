<script>
  let { kind = "error", message } = $props();
</script>

{#if message}
  <p class="notice {kind}" role={kind === "error" ? "alert" : "status"}>{message}</p>
{/if}
