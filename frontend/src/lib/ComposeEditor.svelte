<script>
  import Notice from "./Notice.svelte";
  import { getCompose, runAction, saveCompose } from "./api.js";
  import { loadCodeMirror, shadowMount } from "./codemirror.js";

  let { detail, onClose, onExpired, onStarted, onDirty } = $props();

  let host = $state(null);
  let file = $state(null); // { file, content, etag, editable, writable, self }
  let dirty = $state(false);
  let phase = $state("loading"); // loading | editing | confirm | saving | saved
  let problem = $state(null); // { message, output, conflict }
  let warnings = $state([]);
  let loadError = $state("");

  let view = null;
  let saved = ""; // the text as it is on disk

  // The stack name, not the whole detail object (replaced on every refresh).
  const stack = $derived(detail.name);

  function handle(e) {
    if (e.status === 401) {
      onExpired();
      return true;
    }
    return false;
  }

  async function load() {
    phase = "loading";
    problem = null;
    warnings = [];
    try {
      file = await getCompose(stack);
    } catch (e) {
      if (!handle(e)) loadError = e.message;
      return;
    }
    saved = file.content;
    if (view) view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: saved } });
    setDirty(false);
    phase = "editing";
  }

  function setDirty(value) {
    dirty = value;
    onDirty?.(value);
  }

  $effect(() => {
    if (!host) return;
    let closed = false;
    (async () => {
      const [cm] = await Promise.all([loadCodeMirror(), load()]);
      if (closed || !file) return;
      const { shadow, mount } = shadowMount(host);
      view = new cm.EditorView({
        root: shadow,
        parent: mount,
        state: cm.EditorState.create({
          doc: saved,
          extensions: [
            cm.basicSetup,
            cm.yaml(),
            cm.highlighting,
            cm.EditorState.tabSize.of(2),
            cm.EditorState.readOnly.of(!file.editable),
            cm.keymap.of([{ key: "Mod-s", preventDefault: true, run: () => (askSave(), true) }]),
            cm.EditorView.updateListener.of((u) => {
              if (u.docChanged) setDirty(u.state.doc.toString() !== saved);
            }),
          ],
        }),
      });
    })();
    return () => {
      closed = true;
      view?.destroy();
      view = null;
    };
  });

  function askSave() {
    if (!file?.editable || !dirty || phase === "saving") return;
    problem = null;
    phase = "confirm";
  }

  async function save() {
    phase = "saving";
    problem = null;
    const text = view.state.doc.toString();
    try {
      const result = await saveCompose(stack, text, file.etag);
      // Show the file exactly as written: the server may have added a final newline.
      const written = result.content;
      file = { ...file, etag: result.etag, content: written };
      saved = written;
      if (written !== text) {
        const changes = written.startsWith(text)
          ? { from: text.length, insert: written.slice(text.length) }
          : { from: 0, to: view.state.doc.length, insert: written };
        view.dispatch({ changes });
      }
      warnings = result.warnings;
      setDirty(false);
      phase = "saved";
    } catch (e) {
      if (handle(e)) return;
      problem = {
        message: e.message,
        output: e.detail?.output || [],
        conflict: e.status === 409 && !e.detail?.job,
      };
      phase = "editing";
    }
  }

  async function apply() {
    try {
      const job = await runAction(stack, "start");
      onStarted(job.id);
      phase = "editing";
    } catch (e) {
      if (handle(e)) return;
      if (e.status === 409 && e.detail?.job) onStarted(e.detail.job);
      else problem = { message: e.message, output: [], conflict: false };
    }
  }

  function revert() {
    view?.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: saved } });
    problem = null;
  }

  function close(force = false) {
    if (dirty && !force) {
      phase = "discard";
      return;
    }
    setDirty(false);
    onClose();
  }
</script>

<svelte:window onbeforeunload={(e) => dirty && e.preventDefault()} />

<section class="editor" aria-label="Compose file">
  <div class="logs-bar">
    <h2>{file?.file || detail.compose_file}</h2>
    {#if file && !file.editable}
      <span class="live-state">Read-only</span>
    {:else if dirty}
      <span class="live-state reconnecting">Unsaved changes</span>
    {/if}
    <div class="logs-controls">
      {#if file?.editable}
        <button type="button" class="secondary small" onclick={revert} disabled={!dirty || phase === "saving"}>Revert</button>
        <button type="button" class="small" onclick={askSave} disabled={!dirty || phase === "saving"}>Save</button>
      {/if}
      <button type="button" class="secondary small" onclick={() => close()}>Close</button>
    </div>
  </div>

  {#if loadError}<Notice message={loadError} />{/if}

  {#if file && !file.editable}
    <p class="notice info">
      {#if file.self}
        This is the stack this app runs in. It stays read-only here: change it over SSH.
      {:else}
        The app may not write this file. See docs/compose-editor.md in the QuenWard repository to allow it.
      {/if}
    </p>
  {/if}

  {#if phase === "discard"}
    <div class="confirm">
      <p>Close the editor and discard your unsaved changes?</p>
      <div class="confirm-buttons">
        <button type="button" class="secondary small" onclick={() => (phase = "editing")}>Keep editing</button>
        <button type="button" class="small danger" onclick={() => close(true)}>Discard</button>
      </div>
    </div>
  {:else if phase === "confirm"}
    <div class="confirm">
      <p>
        Save {file.file}? It is checked with <code>docker compose config</code> first, and the
        current version is kept as <code>{file.file}.bak</code>. Running containers do not change
        until you apply it.
      </p>
      <div class="confirm-buttons">
        <button type="button" class="secondary small" onclick={() => (phase = "editing")}>Cancel</button>
        <button type="button" class="small" onclick={save}>Save</button>
      </div>
    </div>
  {:else if phase === "saving"}
    <p class="notice info">Checking and saving…</p>
  {:else if phase === "saved"}
    <div class="confirm saved">
      <p>Saved. Apply it now with <code>docker compose up -d</code>? Only changed services are recreated.</p>
      <div class="confirm-buttons">
        <button type="button" class="secondary small" onclick={() => (phase = "editing")}>Later</button>
        <button type="button" class="small" onclick={apply}>Apply</button>
      </div>
    </div>
  {/if}

  {#if problem}
    <div class="notice error edit-problem">
      <p>{problem.message}</p>
      {#if problem.output.length}<pre>{problem.output.join("\n")}</pre>{/if}
      {#if problem.conflict}
        <button type="button" class="secondary small" onclick={load}>Reload from disk (drops your changes)</button>
      {/if}
    </div>
  {/if}
  {#if warnings.length && phase !== "confirm"}
    <div class="notice info edit-problem">
      <p>docker compose had warnings:</p>
      <pre>{warnings.join("\n")}</pre>
    </div>
  {/if}

  <div class="editor-box" bind:this={host}></div>
</section>
