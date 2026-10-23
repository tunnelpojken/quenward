<script>
  import { STATUS_LABEL } from "./format.js";

  let { stacks, selected, onSelect } = $props();

  let filter = $state("");
  let shown = $derived(
    stacks.filter((s) => s.name.toLowerCase().includes(filter.trim().toLowerCase())),
  );
</script>

<nav class="stack-list" aria-label="Stacks">
  <input
    class="filter"
    type="search"
    placeholder="Filter stacks"
    aria-label="Filter stacks"
    bind:value={filter}
  />
  <ul>
    {#each shown as stack (stack.name)}
      <li>
        <button
          type="button"
          class="stack-item"
          class:active={stack.name === selected}
          aria-current={stack.name === selected ? "page" : undefined}
          onclick={() => onSelect(stack.name)}
        >
          <span class="dot {stack.status}" class:unhealthy={stack.unhealthy} aria-hidden="true"></span>
          <span class="stack-name">{stack.name}</span>
          {#if stack.self}<span class="tag">this app</span>{/if}
          {#if stack.error}<span class="tag bad">error</span>{/if}
          <span class="count" title={STATUS_LABEL[stack.status]}>
            {#if stack.total}{stack.running}/{stack.total}{:else}–{/if}
          </span>
          <span class="sr-only">{STATUS_LABEL[stack.status]}{stack.unhealthy ? ", unhealthy" : ""}</span>
        </button>
      </li>
    {:else}
      <li class="muted empty">{stacks.length ? "No stack matches." : "No stacks found."}</li>
    {/each}
  </ul>
</nav>
