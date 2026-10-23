// CodeMirror, loaded only when the compose editor opens.
//
// CodeMirror styles itself through style-mod, which uses a <style> element in a
// normal page (blocked by our Content-Security-Policy) but constructable
// stylesheets inside a shadow root (allowed). So the editor lives in its own
// shadow root, and our editor CSS is applied there the same way. CSS custom
// properties (the theme colours) still reach inside.

import editorCss from "./editor.css?inline";

export async function loadCodeMirror() {
  const [cm, yaml, state, view, highlight, language] = await Promise.all([
    import("codemirror"),
    import("@codemirror/lang-yaml"),
    import("@codemirror/state"),
    import("@codemirror/view"),
    import("@lezer/highlight"),
    import("@codemirror/language"),
  ]);
  return {
    basicSetup: cm.basicSetup,
    yaml: yaml.yaml,
    EditorState: state.EditorState,
    Compartment: state.Compartment,
    EditorView: view.EditorView,
    keymap: view.keymap,
    // Token classes (tok-keyword, tok-string...) coloured in editor.css, so the
    // highlighting follows the light and dark theme.
    highlighting: language.syntaxHighlighting(highlight.classHighlighter),
  };
}

export function shadowMount(host) {
  const shadow = host.shadowRoot || host.attachShadow({ mode: "open" });
  const sheet = new CSSStyleSheet();
  sheet.replaceSync(editorCss);
  shadow.adoptedStyleSheets = [sheet];
  shadow.replaceChildren();
  const mount = document.createElement("div");
  mount.className = "mount";
  shadow.appendChild(mount);
  return { shadow, mount };
}
