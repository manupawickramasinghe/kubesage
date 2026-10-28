# dashboard/

The AI Control Center's web UI. `kubesage serve` serves this folder as it is: plain HTML, CSS and JavaScript, no
build step, no CDN. Files are read from disk on every request, so after an edit you only reload the page.

| file | what it holds |
|---|---|
| `index.html` | the markup for all five views, the header and the demo caption bar |
| `css/base.css` | colour tokens (light and dark), fonts, header and tabs, buttons, page layout |
| `css/finops.css` | FinOps: figures, the isometric cluster, inspector, charts, recommendations |
| `css/agents.css` | Agent team: test cases, the sub-agent → Thinker → Executor diagram, diagnosis, diff |
| `css/lab.css` | Under the hood: pipeline stages and the shell, journal and wire panes |
| `css/tmux.css` | tmux: the read-only mirror of the RAM ring |
| `css/demo.css` | Demo: the pitch, the run of show and the caption bar |
| `js/core.js` | shared helpers and state: `$`, `esc`, `usd`, icons, `api()`, busy state, the activity log |
| `js/finops.js` | the FinOps view and its three steps |
| `js/lab.js` | Under the hood: pipeline stages, the read-only shell, journal and wire |
| `js/agents.js` | the agent team: test cases, team runs, diagnosis and change |
| `js/tmux.js` | the tmux view |
| `js/demo.js` | the run of show (`DEMO`): each step's view, what to ring, narration, button and live status |
| `js/main.js` | view switching from the URL hash, and start-up. Loaded last |
| `fonts/` | Inter and Instrument Serif (SIL OFL, licences beside them) |

The scripts are classic scripts that share one global scope, loaded in the order `index.html` lists them. Rules
that keep the page safe and consistent are in [docs/HANDOVER.md](../docs/HANDOVER.md#the-ui-in-code). The most
important: model, pod and node text is untrusted, so it reaches `innerHTML` only through `esc()`.

To change the demo, edit `DEMO` in `js/demo.js`. To add a view, add its panel to `index.html` with
`data-panel="<name>"`, a tab, and the name in `VIEWS` and `PANELS` in `js/main.js`.
