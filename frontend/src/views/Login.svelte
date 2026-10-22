<script>
  import { untrack } from "svelte";
  import Card from "../lib/Card.svelte";
  import Notice from "../lib/Notice.svelte";
  import { login } from "../lib/api.js";

  let { onDone, initialUsername = "", info = "" } = $props();

  // Only the starting value is wanted; typing must not be overwritten by the prop.
  let username = $state(untrack(() => initialUsername));
  let password = $state("");
  let error = $state("");
  let busy = $state(false);

  async function submit(event) {
    event.preventDefault();
    error = "";
    busy = true;
    try {
      const session = await login(username.trim(), password);
      password = "";
      onDone(session);
    } catch (e) {
      error = e.message;
      password = "";
    } finally {
      busy = false;
    }
  }
</script>

<Card title="Log in">
  <form onsubmit={submit}>
    <Notice kind="info" message={info} />
    <label>
      Username
      <input bind:value={username} autocomplete="username" spellcheck="false" required maxlength="64" />
    </label>
    <label>
      Password
      <input type="password" bind:value={password} autocomplete="current-password" required maxlength="256" />
    </label>
    <Notice message={error} />
    <button type="submit" disabled={busy}>{busy ? "Logging in…" : "Log in"}</button>
  </form>
  <p class="hint">
    Forgot the password? Run <code>docker exec -it &lt;container&gt; reset-password</code> on the server.
  </p>
</Card>
