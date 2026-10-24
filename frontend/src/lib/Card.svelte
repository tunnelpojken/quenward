<script>
  import ThemePicker from "./ThemePicker.svelte";

  let { title, subtitle = "", children } = $props();
</script>

<main class="center">
  <section class="card">
    <p class="brand">QuenWard</p>
    <h1>{title}</h1>
    {#if subtitle}<p class="muted">{subtitle}</p>{/if}
    {@render children()}
    <div class="card-foot"><ThemePicker /></div>
  </section>
</main>
