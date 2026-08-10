# Chat Landing Redesign — Design

## Goal

Wren's web chat opens on a single line of grey text: *"Ask Wren anything — or
say 'add potatoes to shopping'."* It does not say who you are, does not show
what Wren can do, and gives no way to see or change which model is answering.

This replaces that empty state with a proper landing screen — the Wren mark, a
greeting that knows your name, prompt-starter pills for each skill, and a model
selector in the composer — modelled on the shape of a modern chat client's
new-conversation screen.

## Decisions taken

| Question | Decision | Why |
|---|---|---|
| What the pills do | Drop a starter phrase into the composer; do not send | The phrasing is the thing people do not know. Finishing the sentence teaches it; auto-sending guesses at intent. |
| Where the greeting name comes from | A new settable alias for the owner; their existing contact alias for everyone else | Contacts already carry a usable alias. Only `owner` is a placeholder, so only the owner needs a new setting. |
| How the greeting is produced | Templated, time-aware, browser clock | The landing screen must render instantly and never fail. An LLM-generated greeting would spend a model call before you have typed, and show an error whenever the provider is down. |
| Time of day source | The browser, not `TIMEZONE` | `TIMEZONE` exists so reminders fire correctly on the host. A greeting should reflect where the reader is. |
| Model change scope | Global, owner-only, persisted | Consistent with every other setting, and honest: there is one engine, shared by Discord, reminders and the web chat. |
| Model list source | Live from the active provider, proxied | Always accurate, surfaces a newly pulled model, and keeps the internal LLM endpoint out of the page. |
| `SETTABLE` shape | One record per key, replacing three parallel maps | A third per-key behaviour was the tipping point. See below. |

## Architecture

### The landing state

`showEmpty()` in `wren/communication/chat.html` currently writes one `<div>`.
It becomes: mark, greeting, then the existing composer, then the pills.

The mark is defined **once** as an inline `<symbol>` at the top of `<body>` and
referenced with `<use>` by both the sidebar and the landing header. The
branding work deliberately accepted inlining the geometry; a third copy is where
that stops being acceptable, and `<symbol>`/`<use>` retires the duplication
rather than adding to it.

At roughly 28 px the landing mark stays the one-colour silhouette
(`mark-simple`), for the reason `Brand/README.md` gives: below about 24 px the
full mark's wing, eye and supercilium stop resolving and read as dirt.

### The greeting

`Good {morning|afternoon|evening}, {name}.`

Time of day is computed in the browser from its own clock. The name comes from
the server, because the browser has no idea who the bearer token maps to.

### Identity: `GET /api/me`

A new endpoint, available to **any authenticated whitelisted user** — not
owner-only. This matters: `/api/plugins` is owner-only, so a household member
loading the page would get a 403 and no greeting at all.

```
GET /api/me  ->  {"name": "Erin", "skills": ["shopping_skill", "notes_skill", ...]}
```

`name` resolves as: the new `OWNER_NAME` setting if the caller is the owner,
otherwise their contact alias, title-cased. If `OWNER_NAME` is unset, the
greeting degrades to the time of day alone (`Good evening.`) rather than
addressing someone as "owner".

`skills` is the list of **enabled** skill module names. It is deliberately part
of this endpoint rather than a second call: the pills need it, it is not
sensitive, and it means a pill disappears when its skill is switched off instead
of advertising a capability that will dead-end. Returning it here does not widen
what a non-owner can see in any meaningful way — the same information is already
in Wren's own "what plugins do you have" reply.

### The pills

Starter phrases live in the page, keyed by skill module name, and are rendered
only for modules present in `/api/me`'s `skills`:

| Module | Starter |
|---|---|
| `shopping_skill` | `add ` |
| `notes_skill` | `remember that ` |
| `reminder_skill` | `remind me to ` |
| `pins_skill` | `pin: ` |
| `web_skill` | `look up ` |

`contacts_skill` is deliberately omitted — it is owner-only administration, not
something to invite a household member into.

**A module with no starter renders no pill.** That is the single rule, and it
covers both cases: `contacts_skill` is enabled but has no entry above, so it
never appears; and a skill that is switched off is absent from `skills`, so it
cannot appear either. A new skill added to the registry shows no pill until
someone writes a starter for it, which is the right default — a pill whose
phrasing nobody chose would teach the wrong thing.

Clicking fills the composer, focuses it, and puts the caret at the end. It does
not send.

### One `Setting` record instead of three parallel maps

`wren/config.py` currently keys three separate maps by setting name:
`SETTABLE` (validator), `_BOOT_COERCERS` (a different coercer at boot, for
`NOTIFY_VIA`), and `_SERIALIZE` (the inverse, added after the settings panel
shipped serving Python reprs). Nothing enforces that they agree, which a review
already flagged. Model selection needs a fourth behaviour — how a value is
*applied* — and four parallel maps is not a shape to add to.

```python
class Setting(NamedTuple):
    coerce:      Callable[[str], object]            # validate + parse; raises on bad input
    boot_coerce: Callable | None = None             # when boot cannot do the full check
    serialize:   Callable[[object], str] = str      # inverse, for the settings payload
    apply:       Callable[[str, object], None] = _apply_attr   # default: setattr onto config
```

`SETTABLE` becomes `dict[str, Setting]`. Adding a setting becomes one edit in
one place. The existing behaviours must survive the move unchanged:
`NOTIFY_VIA`'s boot coercion (the router is empty at boot, so the strict check
belongs only on the interactive path) and the `GITHUB_WATCH`/`EMAIL_WATCH`
serializers.

**The security rule is unchanged and still load-bearing:** `SETTABLE` is a
positive allowlist, `GET /api/plugins` returns the values of everything in it,
and no credential may ever be added. Model *names* are not credentials; API
keys are, and stay out.

### Switching the model

The settings machinery cannot drive this as it stands, and that is worth stating
plainly because it contradicts the mental model the last spec established:

- `apply_overrides()` assigns onto the `config` module, and every ordinary
  consumer reads `config.X` at call time.
- But `providers.py` reads `os.environ.get(spec["model_env"])`, not `config`.
- And `config.LLM_CHAIN` is resolved **once at import** (`config.py:67`) into a
  list of dicts with the model string already baked in.

There is no `config.OLLAMA_MODEL` attribute to assign to. So model keys get an
`apply` that writes `os.environ` and then rebuilds.

**Which keys, exactly:** the `model_env` of every provider `providers.py`
supports — `OLLAMA_MODEL`, `LMSTUDIO_MODEL`, `OPENAI_MODEL`, `CLAUDE_MODEL`,
`OPENROUTER_MODEL` — added to `SETTABLE` as five literal entries, not derived
from `LLM_PROVIDERS` at runtime. A literal allowlist is the point: it stays
readable and auditable, and a reviewer can see every settable name in one
place. The matching `*_API_KEY` entries are credentials and are not added.

One consequence worth knowing rather than discovering: `providers.resolve()`
returns `None` when a provider's model is unset, which is how an unconfigured
provider stays out of the chain. Setting `LMSTUDIO_MODEL` from the panel can
therefore *revive* LM Studio into the fallback chain, if it is already named
in `LLM_PROVIDERS` but was missing a model — not merely change its model.
It cannot go further than that: `reload_llm_chain()` only re-resolves
`_provider_names`, captured once at import from `LLM_PROVIDERS`, which is
itself not in `SETTABLE`. Setting the model of a provider `LLM_PROVIDERS`
never named at boot changes nothing observable, and `SETTABLE` cannot tell
the difference — it lists all five `*_MODEL` keys unconditionally, so that
write still reports success. Reviving an already-listed provider is useful
and intended; silently doing nothing for one that was never listed is not,
and the panel does not warn you which case you are in.

```python
def reload_llm_chain() -> None:
    """Re-run exactly what import runs, so a runtime switch and a restart
    cannot diverge."""
```

**Guard:** if a rebuild would produce an empty chain, reject it and keep the
existing one. Wren refusing to boot with no provider is a clear, loud error;
Wren silently losing its last provider at runtime because someone touched a
dropdown is not.

Selecting a model the provider does not actually have is not a new failure
mode — the list is live from that provider, and the existing fallback chain
already handles a provider erroring on a call.

### `GET /api/models`

Owner-only. Proxies the active provider's `/v1/models`.

```
{"provider": "ollama", "current": "qwen2.5:7b-instruct", "models": [...]}
```

Proxied rather than fetched by the browser for two reasons: the browser reaches
nginx and nothing else once Wren is bound to `127.0.0.1`, and it keeps the
internal LLM endpoint out of the page source.

**When the provider is unreachable this returns 200 with an empty list and a
`reason`, never a 5xx.** The landing screen is the first thing a user sees and
must not break because the LLM host is rebooting; the dropdown degrades to the
current model as static text.

The dropdown itself is a `<select>` for the owner and static text for everyone
else, failing closed to text on any error — the same rule the settings gear
already follows.

## Failure modes

| Case | Behaviour |
|---|---|
| `OWNER_NAME` unset | Greeting is the time of day alone; never "Good evening, owner" |
| `/api/me` fails | Greeting falls back to time of day; pills render from no skills, i.e. none |
| `/api/models` fails, 403s, or the provider is down | Dropdown renders as static current-model text |
| A rebuild would empty `LLM_CHAIN` | Rejected, old chain kept, 400 to the caller |
| Selected model missing at call time | Existing provider fallback handles it; `status` still reports the truth |
| Non-owner opens the page | Greeting and pills work; no gear; model shown as static text from `/api/me`, not the owner's editable dropdown |

## Testing

- `/api/me`: owner gets the `OWNER_NAME` setting; a contact gets their own alias
  title-cased; unauthenticated gets 401; unset `OWNER_NAME` omits the name
  rather than returning `"owner"`
- `/api/me` lists only **enabled** skills — disable one and it disappears
- every `SETTABLE` key round-trips `coerce → serialize → coerce` to the same value
- the three consolidated behaviours survive: `NOTIFY_VIA` still validates
  strictly on the interactive path and leniently at boot; `GITHUB_WATCH` and
  `EMAIL_WATCH` still serialize to their parseable form
- no credential is in `SETTABLE` (the existing guard, unchanged)
- `reload_llm_chain()` changes what `brain` uses on the *next* call
- a rebuild that would empty the chain is refused and the previous chain kept
- `/api/models`: 403 for a non-owner; empty list plus `reason` rather than 500
  when the provider is unreachable
- **each pill's starter maps to a real skill module name** — so renaming a
  module cannot leave a pill that silently does nothing
- the mark is defined once as a `<symbol>` and referenced twice, not inlined
  twice

## Explicitly not doing

- **Per-conversation model choice.** One engine serves Discord, reminders and
  the web chat; a model that applies only to browser threads is a confusing
  split, and it needs a column on `conversations`.
- **Switching provider from the UI.** Choosing between ollama and LM Studio
  changes what the fallback chain means, not just which model answers.
- **Auto-sending on a pill click.** The starter is a teaching aid; guessing the
  rest of the sentence is worse than letting someone finish it.
- **An LLM-generated greeting.** Charming, and it puts a model call plus a
  failure mode on the first screen.
- **Display names for contacts beyond their alias.** The alias is already a
  name someone chose. A second field would be two sources of truth.
- **Re-theming the page to the brand palette.** The chat UI's accent is green,
  the brand is clay. Still a separate decision, still not this one.
