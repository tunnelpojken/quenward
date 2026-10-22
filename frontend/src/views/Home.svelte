<script>
  import Card from "../lib/Card.svelte";
  import Notice from "../lib/Notice.svelte";
  import { logout } from "../lib/api.js";

  let { session, onLoggedOut } = $props();

  let error = $state("");
  let busy = $state(false);

  async function doLogout() {
    busy = true;
    error = "";
    try {
      await logout();
      onLoggedOut();
    } catch (e) {
      error = e.message;
    } finally {
      busy = false;
    }
  }
</script>

<Card title="Logged in" subtitle="Stacks, logs and controls arrive in Phase 3.">
  <p>Signed in as <strong>{session.username}</strong>.</p>
  <p class="muted">You are logged out automatically after 30 minutes without activity, and after 3 hours in any case.</p>
  <Notice message={error} />
  <button type="button" class="secondary" onclick={doLogout} disabled={busy}>
    {busy ? "Logging out…" : "Log out"}
  </button>
</Card>
