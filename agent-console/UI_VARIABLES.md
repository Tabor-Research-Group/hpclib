# UI variables

Choose **Open Stylesheet** from the top-left hamburger menu to create and open your `user.css`.
The console copies [`default-styles.css`](default-styles.css) only when your file is absent;
an existing file is preserved. Your operating system chooses its default CSS application.

Set these custom properties on `:root` in `~/.config/hpclib/console/user.css`
(or `$HPCLIB_CONSOLE_DIR/user.css`). This lists every UI variable used by the bundled console.
Fonts must already be installed or supplied by your stylesheet; a font-family setting does not install a font.

## Log panels

`--log-max-width` defaults to `960px` and caps the **whole panel**, including its frame.
Panels are also clamped to their container and viewport; increasing this value cannot force them wider.
Tunnel/app logs, installation/setup output, and Activity job output previews use the same constraint.
Long lines scroll horizontally inside the log by default. To wrap them, use both wrapping settings:

```css
:root {
  --log-max-width: 800px;
  --log-max-height: 360px;
  --log-white-space: pre-wrap;
  --log-overflow-wrap: anywhere;
}
```

Reload the browser page after editing. The console's Refresh button refreshes data only.

## Defaults

The defaults are defined in `default-styles.css`, loaded before the UI rules in `style.css` and your overrides.
Values below are the light-theme defaults. Aliases such as `var(--space-sm)` follow the referenced variable
unless you override the alias separately. Spacing, radii and font sizes remain independent.

### Colours

| Variable | Default | Controls |
| --- | --- | --- |
| `--bg` | `#ffffff` | Page, inputs, panel and dialog background |
| `--fg` | `#1d1d1f` | Main text |
| `--muted` | `#6e6e73` | Secondary text and hovered control borders |
| `--line` | `#e3e3e6` | Borders and table dividers |
| `--panel` | `#f6f6f8` | Controls, logs and toolbar background |
| `--accent` | `#2f6fdb` | Primary actions, links and badges |
| `--good` | `#1f8a4c` | Success, running states and additions |
| `--warn` | `#b36b00` | Starting and queued states |
| `--bad` | `#c62828` | Errors, failures and deletions |
| `--accent-fg` | `#fff` | Text on primary actions and badges |
| `--separator` | `rgba(127, 127, 127, 0.25)` | Package, secret and token section dividers |
| `--viewer-bg` | `#fff` | Image and HTML viewer background |
| `--dialog-backdrop` | `rgb(0 0 0 / 0.35)` | Modal overlay colour |
| `--topbar-bg` | `#16171a` | App bar background |
| `--topbar-fg` | `#e6e6e8` | App bar text |
| `--topbar-muted` | `#b8b8be` | Inactive app links |
| `--topbar-hover-bg` | `#2a2b30` | Hovered app link and package button background |
| `--topbar-active-bg` | `#34363d` | Active app link and hovered package button background |
| `--topbar-active-fg` | `#fff` | Active and hovered app bar text |
| `--topbar-button-fg` | `#e8e8ea` | Package button text |
| `--topbar-button-border` | `#44464e` | Package button border |

### Typography

| Variable | Default | Controls |
| --- | --- | --- |
| `--font-ui` | `-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif` | UI and component close-button font family |
| `--font-mono` | `ui-monospace, Menlo, monospace` | Logs, code, textareas, hashes and token values |
| `--font-size-base` | `14px` | Main UI and saved-result text size |
| `--font-size-small` | `12px` | Badges, hints, errors and small text |
| `--font-size-code` | `12px` | Preformatted text, textareas and hashes |
| `--font-size-heading` | `16px` | Page h2 headings |
| `--font-size-subheading` | `15px` | h3 headings, dialog headings and inline dismiss buttons |
| `--font-size-caption` | `13px` | h4 headings and app bar text |
| `--font-size-label` | `11px` | Tunnel toolbar labels |
| `--close-font-size` | `18px` | Panel and dialog close symbols |
| `--line-height-base` | `1.45` | Main UI line height (unitless) |
| `--line-height-code` | `1.4` | Code and textarea line height (unitless) |
| `--font-weight-medium` | `500` | Table and field labels |
| `--font-weight-semibold` | `600` | Active links, states and panel headings |
| `--font-weight-bold` | `700` | App bar brand |

### Corners

| Variable | Default | Controls |
| --- | --- | --- |
| `--radius-control` | `6px` | Buttons, inputs, logs, toolbars and close controls |
| `--radius-panel` | `8px` | Cards and panel frames |
| `--radius-dialog` | `10px` | Dialog frames |
| `--radius-badge` | `9px` | Count badges |

### Spacing

| Variable | Default | Controls |
| --- | --- | --- |
| `--space-xs` | `4px` | Small margins and row gaps |
| `--space-sm` | `6px` | Small gaps and control spacing |
| `--space-md` | `8px` | Text, log and section spacing |
| `--space-lg` | `12px` | Toolbar gaps and section margins |
| `--space-xl` | `16px` | Page padding, navigation gaps and large margins |
| `--action-gap` | `var(--space-sm)` | Action-button and result gaps |
| `--control-padding` | `3px 10px` | Button and button-link padding |
| `--input-padding` | `3px 6px` | Input and select padding |
| `--cell-padding` | `6px 8px` | Table cell padding |
| `--panel-padding` | `10px 14px 12px` | Panel frame padding |
| `--dialog-padding` | `16px 18px` | Dialog frame padding |
| `--card-padding` | `12px 16px` | Card padding |

### Sizes and behaviour

| Variable | Default | Controls |
| --- | --- | --- |
| `--content-max-width` | `1200px` | Main content width, including page padding |
| `--dialog-max-width` | `440px` | Dialog width before the viewport clamp |
| `--dialog-viewport-gap` | `32px` | Total horizontal clearance around dialogs |
| `--close-size` | `24px` | Close-button width, height and line height |
| `--code-max-height` | `420px` | Ordinary preformatted blocks height cap |
| `--log-max-width` | `960px` | Outer tunnel and operation log panel width cap (length or percentage) |
| `--log-max-height` | `var(--code-max-height)` | Log text area height cap |
| `--log-white-space` | `pre` | Use pre for internal horizontal scrolling or pre-wrap for wrapped logs |
| `--log-overflow-wrap` | `normal` | Use anywhere with pre-wrap to wrap unbroken log lines |
| `--viewer-height` | `70vh` | HTML viewer height and text-file height cap |
| `--action-result-max-width` | `360px` | Inline action result width cap |
| `--disabled-opacity` | `0.5` | Disabled button opacity |

### Dark theme overrides

The following variables change when the operating system prefers dark mode.
All other defaults stay the same. A later `:root` declaration in `user.css` overrides either theme;
use your own dark-mode media query when you want distinct light and dark values.

| Variable | Dark default |
| --- | --- |
| `--bg` | `#18181a` |
| `--fg` | `#ececee` |
| `--muted` | `#9a9aa0` |
| `--line` | `#333338` |
| `--panel` | `#222226` |
| `--accent` | `#6ea0ff` |
| `--good` | `#4cc27f` |
| `--warn` | `#e6a23c` |
| `--bad` | `#ff6b6b` |

## Component styling

Variables inherit into panel and dialog shadow DOM. Component defaults have fallbacks when
the main stylesheet is absent. For changes beyond these variables, use normal selectors or these parts:

- `hpc-panel::part(...)`: `panel`, `header`, `heading`, `status`, `body`, `close`.
- `hpc-dialog::part(...)`: `dialog`, `heading`, `close`.

Other structural CSS (display modes, alignment and table layout) remains in `style.css`.
The log width constraint prevents log content from widening a table; unrelated wide table columns
can still need scrolling on a small screen.
