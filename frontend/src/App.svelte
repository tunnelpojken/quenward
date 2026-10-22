<script>
  // Phase 1 placeholder: proves the frontend is built, served and can reach the backend.
  let status = $state("checking");

  async function check() {
    try {
      const response = await fetch("/healthz");
      status = response.ok && (await response.text()) === "ok" ? "online" : "error";
    } catch {
      status = "offline";
    }
  }

  check();
</script>

<main>
  <section class="card">
    <h1>docker-project</h1>
    <p class="muted">Phase 1 skeleton. Stacks, logs and controls arrive in later phases.</p>
    <p>
      Backend:
      <span class="status" class:ok={status === "online"} class:bad={status !== "online" && status !== "checking"}>
        {status}
      </span>
    </p>
  </section>
</main>

<style>
  main {
    display: grid;
    place-items: center;
    min-height: 100vh;
    padding: 16px;
  }

  .card {
    width: 100%;
    max-width: 420px;
    padding: 24px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
  }

  h1 {
    margin: 0 0 8px;
    font-size: 1.25rem;
  }

  .muted {
    color: var(--muted);
    margin: 0 0 16px;
  }

  .status {
    font-weight: 600;
  }

  .ok {
    color: var(--ok);
  }

  .bad {
    color: var(--bad);
  }
</style>
