<script>
  import Card from "../lib/Card.svelte";
  import Notice from "../lib/Notice.svelte";
  import { resetPassword } from "../lib/api.js";

  let { token, onDone } = $props();

  let password = $state("");
  let confirm = $state("");
  let error = $state("");
  let busy = $state(false);

  async function submit(event) {
    event.preventDefault();
    error = "";
    if (password !== confirm) {
      error = "The passwords do not match.";
      return;
    }
    busy = true;
    try {
      const result = await resetPassword(token, password);
      onDone(result.username);
    } catch (e) {
      error = e.message;
    } finally {
      busy = false;
    }
  }
</script>

<Card title="Choose a new password" subtitle="This link works once and expires 15 minutes after it was created.">
  <form onsubmit={submit}>
    <label>
      <span>New password <span class="muted">(at least 12 characters)</span></span>
      <input type="password" bind:value={password} autocomplete="new-password" required minlength="12" maxlength="256" />
    </label>
    <label>
      Repeat new password
      <input type="password" bind:value={confirm} autocomplete="new-password" required maxlength="256" />
    </label>
    <Notice message={error} />
    <button type="submit" disabled={busy}>{busy ? "Saving…" : "Set new password"}</button>
  </form>
</Card>
