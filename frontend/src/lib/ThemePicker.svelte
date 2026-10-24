<script>
  import { THEMES, currentTheme, setTheme } from "./theme.js";

  let theme = $state(currentTheme());
</script>

<label class="theme-picker">
  <span class="sr-only">Theme</span>
  <select
    bind:value={theme}
    onchange={() => setTheme(theme)}
    title="Theme (remembered in this browser)"
  >
    {#each THEMES as t (t.id)}
      <option value={t.id}>{t.label}</option>
    {/each}
  </select>
</label>
