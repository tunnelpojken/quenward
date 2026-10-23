<script>
  import Card from "./lib/Card.svelte";
  import Notice from "./lib/Notice.svelte";
  import { getSession, setupStatus } from "./lib/api.js";
  import Dashboard from "./views/Dashboard.svelte";
  import Login from "./views/Login.svelte";
  import Reset from "./views/Reset.svelte";
  import Setup from "./views/Setup.svelte";

  // loading | setup | login | reset | home | error
  let view = $state("loading");
  let session = $state(null);
  let resetToken = $state("");
  let loginUsername = $state("");
  let loginInfo = $state("");
  let fatal = $state("");

  function showLogin(info = "", username = "") {
    session = null;
    loginInfo = info;
    loginUsername = username;
    view = "login";
  }

  async function start() {
    // Reset links look like /#/reset/<token>. The token lives in the fragment, which
    // browsers never send to the server, and is removed from the address bar at once
    // so it does not stay in the history.
    const match = location.hash.match(/^#\/reset\/([A-Za-z0-9_-]+)$/);
    if (match) {
      resetToken = match[1];
      history.replaceState(null, "", location.pathname);
      view = "reset";
      return;
    }
    try {
      if (await setupStatus()) {
        view = "setup";
        return;
      }
      session = await getSession();
      if (session) view = "home";
      else showLogin();
    } catch (e) {
      fatal = e.message || "Could not reach the server.";
      view = "error";
    }
  }

  // Coming back to the tab: check that the session has not expired meanwhile.
  async function onVisible() {
    if (document.visibilityState !== "visible" || view !== "home") return;
    try {
      // Only a check: looking at the tab is not using the app, so this must not
      // keep the session alive (it would reset the 30-minute idle timer).
      if (!(await getSession({ background: true }))) {
        showLogin("Your session expired. Please log in again.");
      }
    } catch {
      // Network hiccup: keep the current view; the next action will tell.
    }
  }

  start();
</script>

<svelte:document onvisibilitychange={onVisible} />
<!-- A reset link pasted into a tab where the app is already open only changes the
     #fragment, which does not reload the page. -->
<svelte:window onhashchange={() => location.hash.startsWith("#/reset/") && start()} />

{#if view === "loading"}
  <main class="center"><p class="muted">Loading…</p></main>
{:else if view === "setup"}
  <Setup onDone={(username) => showLogin("Admin account created. You can log in now.", username)} />
{:else if view === "login"}
  <Login
    initialUsername={loginUsername}
    info={loginInfo}
    onDone={(s) => {
      session = s;
      view = "home";
    }}
  />
{:else if view === "reset"}
  <Reset token={resetToken} onDone={(username) => showLogin("Password changed. Log in with the new one.", username)} />
{:else if view === "home"}
  <Dashboard
    {session}
    onLoggedOut={() => showLogin("You have been logged out.")}
    onExpired={() => showLogin("Your session expired. Please log in again.")}
  />
{:else}
  <Card title="Something is wrong">
    <Notice message={fatal} />
  </Card>
{/if}
