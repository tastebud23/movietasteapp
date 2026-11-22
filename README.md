# Tastebud

A personal movie picker that helps you choose something worth watching. **For me** opens first and gives you three worthwhile picks from your favourite films. **With someone** keeps the two-person flow, with one shared pick and two alternatives.

The current personal build is on [`codex/tastebud-personal-core`](https://github.com/tastebud23/movietasteapp/tree/codex/tastebud-personal-core).

## What this build does

- Uses your full favourite-film list, including long numbered lists. It removes list numbering without changing numeric titles such as `1917`.
- Suggests three distinct films, with the strongest first. Every pick should make sense for your taste; there is no forced wildcard.
- Explains each solo pick through a named favourite and a specific connection. Shared picks give a reason for each viewer.
- Keeps **Tonight’s mood** separate from your saved taste. Changing it clears the old results.
- Remembers earlier suggestions during the session. **Try three more** or **Try another pick** asks for fresh films. **Start a fresh movie session** clears session suggestion history without deleting profiles or saved feedback.
- Supports watchlists, watched films and explicit liked/disliked feedback. Saving means interest; marking a film seen does not imply enjoyment. **Not tonight** only skips it for the current session.
- Lets you record a film you watched outside Tastebud, then say whether you liked it. Older watched films can be found by title.

Streaming availability and catalogue lookups are deferred. An existing Watchmode key is left unused. This build has no service filters, availability claims, taste radar or separate blueprint-generation step. Film details are generated and may need checking.

## Run or update your copy

If the previous test copy already works, this update needs **no new dependencies or new API key**. See [UPDATE TEST COPY.txt](UPDATE%20TEST%20COPY.txt) for the Windows update steps.

The recommended route is to download the current branch as a ZIP and extract it into a separate folder. Keep the working copy, its `.venv` and `.streamlit/secrets.toml`. You can copy your existing configuration into the new folder locally and use your existing Python environment.

Run from the folder containing `app_ui.py`. If your existing Windows environment sits one folder above the app, the previously used command is:

```powershell
..\.venv\Scripts\python.exe -m streamlit run app_ui.py
```

If `.venv` sits inside the app folder, use `.venv\Scripts\python.exe` instead.

For a fresh Windows setup with Python 3.12:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
.venv\Scripts\python.exe -m streamlit run app_ui.py
```

For a fresh macOS/Linux setup:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-lock.txt
.venv/bin/python -m streamlit run app_ui.py
```

Python 3.12 is the development version used for validation. `requirements-lock.txt` contains the existing pinned environment; dependency changes are not part of this update. [START HERE.txt](START%20HERE.txt) gives a short first-run walkthrough.

## Configuration

Keep the configuration that already works. Use environment variables or a local `.streamlit/secrets.toml`; do not commit or share secret values.

| Setting | Purpose |
| --- | --- |
| `OPENAI_API_KEY` | Existing OpenAI key, from an environment variable or Streamlit secrets |
| `MOVIETASTE_API_KEY` | Environment-variable alias for the OpenAI key |
| `OPENAI_MODEL` | Existing model override; defaults to `gpt-4o-mini` |
| `SUPABASE_URL` | Optional Supabase project URL for signed-in accounts |
| `SUPABASE_PUBLISHABLE_KEY` | Publishable or legacy anon key for signed-in accounts |
| `WATCHMODE_API_KEY` | May remain in your configuration; unused by this build |

You can open the app, create temporary profiles and view the labelled pair example without an OpenAI key. Personalised picks use your existing key. Supabase is optional for trying the app. The older `SUPABASE_KEY` setting is not used for account authentication; use the publishable/anon key, not a service-role key.

## Profiles and feedback

**Guest profiles last for the browser session.** Refreshing or disconnecting can discard them. **Signed-in profiles use Supabase** and persist across sessions. Signing in or out clears the current session; guest profiles are not imported automatically.

No SQL migration is required when updating an existing installation of this repository’s `movietaste_*` schema. New feedback is stored as versioned events in `movietaste_recommendation_events`. Existing `movietaste_movie_feedback` rows are retained and read as the baseline. New events distinguish a saved film, a watched film, a like, a dislike and an updated favourites list without rewriting those older rows.

Older, unprefixed tables such as `profiles` and `movie_feedback` remain untouched and are not read by this app. This update does not migrate those original profiles. Older app versions will not interpret the new feedback events; use this build to view or edit feedback recorded after the update.

If setting up signed-in storage for the first time, use [supabase/schema.sql](supabase/schema.sql), enable email/password authentication in Supabase, then configure the project URL and publishable key. Existing configured accounts use **Your movie space** in the sidebar.

## Request behaviour

A valid recommendation round uses **one OpenAI request**. A malformed or invalid response can trigger **one additional validation retry**; invalid picks are withheld if that retry also fails. Each request has a 25-second timeout and SDK retries are disabled. API failures do not trigger automatic retry loops. The timeout applies per request, not to the complete interaction.

The full supplied favourite lists go into the request. There is no separate AI taste-blueprint call or catalogue enrichment step. Saving, marking watched and giving feedback make no AI request.

The code checks the output structure, repeated or excluded titles and named favourite anchors. Those checks cannot establish whether the film or explanation will feel right to you.

## Check the build

From the configured Python environment:

```bash
python -m pytest -q
python -m pip check
```

Tests cover full-list handling, output validation, repeat avoidance, feedback meanings, profile behaviour, guest isolation and mocked account interactions. They also check that a configured Watchmode key does not activate catalogue requests.

With a local app running and Node, Playwright and Chromium available:

```bash
node tests/browser_smoke.cjs
```

`CHROMIUM_PATH`, `MOVIETASTE_BASE_URL` and `MOVIETASTE_SCREENSHOTS` can override browser-test settings. The browser check covers the solo landing, pair flow and labelled example on desktop and mobile.

Automated provider and account checks use mocks. Live credentials, authentication, response speed and subjective recommendation quality still need a home test. Use [the short recommendation check](docs/recommendation-checks.md) with your usual favourite list and someone you watch with.
