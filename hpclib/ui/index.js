// hpclib's shared page elements and helpers: what the console and the Workspace are built from. Import this one
// file; it defines every custom element it exports.
//
//   import { el, action, toast, showMenu, loadLib } from "hpclib-ui/index.js";     (the Workspace: an import map)
//   import { el, action } from "./hpclib-ui/index.js";                              (the console)
//
// Styles: load default-styles.css (the variables), then base.css, then your page's own.

export { configure, api, el, action, css, isDark, onThemeChange, remember, CLOSE_CSS } from "./core.js";
export { configureLibs, loadLib, DEFAULT_CDN } from "./libs.js";
export { HpcPanel } from "./components/panel.js";
export { HpcLog } from "./components/log.js";
export { HpcDialog, confirmDialog, promptDialog } from "./components/dialog.js";
export { HpcState } from "./components/state.js";
export { HpcField } from "./components/field.js";
export { HpcActionButton } from "./components/action-button.js";
export { HpcEnvEditor } from "./components/env-editor.js";
export { HpcTabs } from "./components/tabs.js";
export { HpcSplit } from "./components/split.js";
export { HpcMenu, showMenu } from "./components/menu.js";
export { HpcToasts, toast } from "./components/toast.js";
export { HpcCode, HpcDiff, languageFor } from "./components/code.js";
export { HpcTerminal } from "./components/terminal.js";
export { HpcFileBrowser } from "./components/file-browser.js";
export { HpcResources } from "./components/resources.js";
export { parseDuration, formatDuration, parseMemory, formatMemory } from "./units.js";
