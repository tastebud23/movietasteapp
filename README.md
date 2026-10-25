# Movie Taste

Two tastes. One movie. A Streamlit app that helps two people choose a film, with optional taste profiles and watchlists.

This repository incorporates the newer uploaded source, including Watchmode metadata, Supabase integration, taste analysis and recommendation guardrails.

## Run locally

Python 3.12 is the validated development version.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-lock.txt
python -m streamlit run app_ui.py
```

Run from the repository root to load the theme. `requirements.txt` declares app dependencies; `requirements-dev.txt` adds pytest; `requirements-lock.txt` pins the validated development environment, including transitive and test dependencies. Refresh the lock deliberately after dependency changes.

No credentials are required to open the app, create session-only profiles or explore the labelled example. Personalised recommendations require an OpenAI key. Guest sessions are temporary: refreshing/disconnecting can discard profiles. Signing in or out clears the session; guest profiles are not automatically imported.

## Configuration

Use environment variables or Streamlit secrets. Never commit secret values. Cloud environments can bind `MOVIETASTE_API_KEY` instead of the reserved `OPENAI_API_KEY`; the app accepts either without writing credentials to disk.

| Name | Purpose |
| --- | --- |
| `MOVIETASTE_API_KEY` or `OPENAI_API_KEY` | Required for personalised recommendations |
| `OPENAI_MODEL` | Responses API model; defaults to `gpt-4o-mini` |
| `WATCHMODE_API_KEY` | Optional catalogue metadata and availability |
| `SUPABASE_URL` | HTTPS project URL for persistent accounts |
| `SUPABASE_PUBLISHABLE_KEY` | Publishable key or legacy **anon** key for persistent accounts |
| `ENABLE_DEBUG` | Set `true` for local development diagnostics; off by default |

The legacy `SUPABASE_KEY` variable is deliberately not used. Do not supply service-role or `sb_secret_` keys for authentication. The client key check only checks format/role; Supabase verifies credentials.

Network destinations: `api.openai.com`, `api.watchmode.com`, your exact Supabase project hostname and poster hosts returned by Watchmode. Installation uses PyPI. Keep TLS verification enabled.

## Account-backed storage

1. Run [supabase/schema.sql](supabase/schema.sql) in your Supabase SQL editor.
2. Enable Supabase email/password authentication and configure confirmation emails/site settings for deployment.
3. Supply `SUPABASE_URL` and `SUPABASE_PUBLISHABLE_KEY` through deployment configuration.
4. Open **Your movie space** in the sidebar, create an account, confirm email if required, and sign in.

New `movietaste_*` tables use composite keys containing `owner_id`, row-level security and authenticated-user policies. The app also scopes reads/deletes and attaches the authenticated owner to writes. Clients and guest data belong to individual sessions; user data and authenticated clients are never globally cached.

Legacy tables remain untouched and are not read by this app. Existing profiles therefore do not appear automatically. Plan an explicit, backed-up migration after establishing ownership of old profiles. Audit legacy API grants and RLS before public release: new policies do not secure old tables.

The SQL is rerunnable for the new schema; it does not upgrade incompatible existing tables with the same names.

## Behaviour

- Manual entry is the default. Saved-profile selection appears after two profiles exist.
- One shared pick appears first, followed by alternatives without repeating the top pick.
- Editing either viewer or viewing preferences clears stale results. Every successful shared pick is remembered for the full browser session, even across input changes. Use **Start a fresh movie session** to clear that history.
- Shared picks use structured JSON output with three distinct slots and a best pick drawn from those slots. Invalid responses, blocked titles and invented candidate IDs trigger one retry; persistently invalid results are withheld. Each pick explains its connection to each viewer and a possible drawback. Each explanation must explicitly mention a favourite from the correct viewer; this check rejects unanchored or misattributed explanations, but does not prove the reasoning is accurate. Checked favourite plots are supplied to the model when available.
- Watchlist saving needs no extra AI request.
- With Watchmode connected, shared picks are selected only from catalogue entries retrieved for both viewers and a broader discovery lane. Genre-based ranking balances both viewers and diversifies the shortlist; this is a heuristic, not a measured enjoyment score.
- Selected streaming services constrain shared recommendations to matching country/service metadata. Optional runtime and family restrictions also filter candidates before AI selection; unknown metadata fails these checks. Family mode uses US G/PG ratings as a guide, not a guarantee of suitability for an individual child. Rentals/purchases remain opt-in. Solo availability controls still affect labels only.
- Without Watchmode, unrestricted shared picks can use explicitly labelled AI-only suggestions. Family/runtime/service restrictions require the catalogue. Catalogue failures or too few matches do not silently relax limits or fall back to unverified suggestions.
- Catalogue retrieval samples 1900–2004, 2005–2017 and 2018–today separately for each discovery lane, interleaves the results, enriches at most 30 candidate entries (plus up to six seeds), and caches successful session lookups for 15 minutes. It stops starting more lookups after a 75-second search budget; a request already in progress can take longer to finish. Later rounds advance discovery pages. Availability can change; a limited search is not exhaustive proof that no matching film exists.
- Recommendation rules, catalogue retrieval and structured results rendering live in `pair_recommendations.py`, `pair_catalogue.py` and `pair_ui.py`. The legacy solo flow remains in `app_ui.py`.
- API errors omit sensitive request details. OpenAI calls have timeouts, bounded output and a per-session throttle. This is not a deployment-wide spending limit: add provider budgets and a shared rate limiter before unrestricted public use.

## Validation

```bash
python -m pytest -q
python -m pip check
```

Tests cover guest isolation, profile persistence/deletion, owner-scoped database requests, key rejection, country/subscription filtering, example rendering, stale results, rerolls, watchlist saving, failed/incomplete model output and state clearing on sign-in/out.

AI and authentication tests use mocks. They do not prove live API access, database policy enforcement or recommendation quality.

Optional browser check with Node, Playwright and Chromium installed, after starting the app:

```bash
node tests/browser_smoke.cjs
```

Override `CHROMIUM_PATH`, `MOVIETASTE_BASE_URL` or `MOVIETASTE_SCREENSHOTS` as needed. Screenshots go outside the checkout by default.

Use [docs/recommendation-checks.md](docs/recommendation-checks.md) to compare real recommendation quality. Automated checks validate rules and integration mechanics, not whether people will enjoy a film.

## Before a public beta

- Validate email confirmation, login, token refresh and logout against real Supabase.
- With two real users, verify each can use the same profile name and cannot read/update/delete the other's rows, including direct REST requests and forged owner IDs.
- Make real OpenAI recommendations; verify the model and Watchmode country/service metadata.
- Add deployment-wide budgets, rate limits and operational monitoring. Review privacy/retention before storing histories or adding analytics.
- Observe several pairs choosing films. Measure time to choose, acceptance, whether they watched it and whether both enjoyed it. No third-party analytics has been added.

The guest workflow is locally validated. Live integrations, production RLS enforcement, deployment, spending controls and real-user recommendation quality require separate validation.
