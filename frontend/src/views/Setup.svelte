<script>
  import Card from "../lib/Card.svelte";
  import Notice from "../lib/Notice.svelte";
  import { setup } from "../lib/api.js";

  let { onDone } = $props();

  let token = $state("");
  let username = $state("");
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
      await setup(token.trim(), username.trim(), password);
      onDone(username.trim());
    } catch (e) {
      error = e.message;
    } finally {
      busy = false;
    }
  }
</script>

<Card title="Create the admin account" subtitle="This page works only once, with the setup token from the container log.">
  <p class="hint top">
    Find the token with
    <code>docker compose logs app | grep "setup token"</code>
  </p>
  <form onsubmit={submit}>
    <label>
      Setup token
      <input bind:value={token} autocomplete="off" spellcheck="false" required maxlength="128" />
    </label>
    <label>
      Username
      <input bind:value={username} autocomplete="username" spellcheck="false" required maxlength="64" />
    </label>
    <label>
      <span>Password <span class="muted">(at least 12 characters)</span></span>
      <input type="password" bind:value={password} autocomplete="new-password" required minlength="12" maxlength="256" />
    </label>
    <label>
      Repeat password
      <input type="password" bind:value={confirm} autocomplete="new-password" required maxlength="256" />
    </label>
    <Notice message={error} />
    <button type="submit" disabled={busy}>{busy ? "Creating…" : "Create admin"}</button>
  </form>
</Card>
