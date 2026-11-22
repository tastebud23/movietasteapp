"""Tastebud's personal movie picker and two-person flow.

Recommendation rules and feedback history live in independently tested modules.
Catalogue/streaming lookups are deferred; an existing Watchmode key is left unused.
"""
import json
import os
import time
from datetime import datetime, timezone

import streamlit as st
from openai import OpenAI, RateLimitError, APITimeoutError, APIError

from accounts import storage_for_session
from feedback import derive_profile_feedback, make_feedback_event
from pair_recommendations import RecommendationError, select_pair, title_key, to_display_text
from pair_ui import render_selection
from solo_helpers import clean_title
from solo_recommendations import select_solo
from storage import read_all

st.set_page_config(page_title="Tastebud", page_icon="🎬", layout="centered", initial_sidebar_state="collapsed")


def get_secret_value(key, default=""):
    aliases = {"OPENAI_API_KEY": "MOVIETASTE_API_KEY"}
    value = os.environ.get(aliases.get(key, key)) or os.environ.get(key)
    if value:
        return value.strip()
    try:
        return str(st.secrets.get(key, default)).strip()
    except Exception:
        return default


OPENAI_API_KEY = get_secret_value("OPENAI_API_KEY")
SUPABASE_URL = get_secret_value("SUPABASE_URL")
SUPABASE_PUBLISHABLE_KEY = get_secret_value("SUPABASE_PUBLISHABLE_KEY")
OPENAI_MODEL = get_secret_value("OPENAI_MODEL", "gpt-4o-mini")
# One request, with at most one explicit validation retry in the selection module.
client = OpenAI(api_key=OPENAI_API_KEY, timeout=25.0, max_retries=0) if OPENAI_API_KEY else None
supabase = storage_for_session(SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY)


def call_openai_text(prompt, schema=None):
    if client is None:
        st.info("Connect your existing OpenAI key to get personalised picks. Profiles and the example work without it.")
        return ""
    now = time.monotonic()
    calls = [t for t in st.session_state.get("ai_call_times", []) if now - t < 60]
    if len(calls) >= 20:
        st.warning("You’ve made several requests. Give it a minute before trying again.")
        return ""
    st.session_state.ai_call_times = calls + [now]
    options = {}
    if schema:
        options["text"] = {"format": {"type": "json_schema", "name": "movie_selection", "strict": True, "schema": schema}}
    try:
        response = client.responses.create(model=OPENAI_MODEL, input=prompt, max_output_tokens=2400, **options)
        return response.output_text.strip()
    except RateLimitError:
        st.error("Movie picks are temporarily unavailable. Please try again in a moment.")
    except APITimeoutError:
        st.error("The movie request timed out. Please try again.")
    except APIError:
        st.error("OpenAI had a temporary API issue. Please try again shortly.")
    except Exception:
        st.error("We couldn’t finish your movie picks. Please try again.")
    return ""


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def dedupe_titles(titles):
    result, seen = [], set()
    for title in titles:
        key = title_key(title)
        if key and key not in seen:
            result.append(title.strip())
            seen.add(key)
    return result


def parse_movie_input(text):
    return dedupe_titles([clean_title(line) for line in str(text or "").splitlines() if clean_title(line)])


def empty_profile_record():
    return derive_profile_feedback([], []) | {"taste_summary": "", "latest_taste_read": ""}


def load_profiles():
    try:
        profile_rows = read_all(supabase, "profiles")
        legacy_rows = read_all(supabase, "movie_feedback")
        feedback_events = read_all(supabase, "recommendation_events", filters={"event_type": "feedback"})
    except Exception:
        st.error("Your profiles could not be loaded. Check your connection or try signing in again.")
        st.stop()
    return {
        row["profile_name"]: derive_profile_feedback(
            [f for f in legacy_rows if f.get("profile_name") == row["profile_name"]],
            [e for e in feedback_events if e.get("profile_name") == row["profile_name"]],
        ) | {"taste_summary": row.get("taste_summary", ""), "latest_taste_read": row.get("latest_taste_read", "")}
        for row in profile_rows
    }


def record_feedback(profile_name, action, movie_title="", *, titles=None, slot=""):
    try:
        event = make_feedback_event(profile_name, action, movie_title=movie_title, titles=titles, slot=slot)
        supabase.table("recommendation_events").insert(event).execute()
        return True
    except Exception:
        st.error("That feedback could not be saved. Your earlier feedback is still there; please try again.")
        return False


def update_profile_feedback(profile_name, movie_title, bucket, slot=""):
    actions = {"saved_movies": "save", "seen_movies": "seen", "disliked_movies": "dislike"}
    return record_feedback(profile_name, actions[bucket], movie_title, slot=slot)


def create_recommendation_event(profile_name, movies, output, titles, *, event_type="solo", names=None, best_pick=None):
    data = {
        "profile_name": profile_name, "input_movies": "\n".join(movies), "output_text": output,
        "recommended_titles": titles, "created_at": utc_now_iso(), "event_type": event_type,
        "reroll_count": st.session_state.get("solo_reroll" if event_type == "solo" else "pair_reroll", 0),
        "profile_1_name": names[0] if names else None, "profile_2_name": names[1] if names else None,
        "best_pick": best_pick,
    }
    try:
        supabase.table("recommendation_events").insert(data).execute()
    except Exception:
        st.warning("Your picks are ready, but this round could not be added to your saved history. They will still be remembered for this session.")


def recent_recommended_titles(profile_name):
    if not profile_name:
        return []
    try:
        events = read_all(supabase, "recommendation_events")
    except Exception:
        st.caption("Saved recommendation history is unavailable; this session’s earlier picks are still excluded.")
        return []
    # Feedback never counts as a recommendation round. Joint picks belong to both viewers.
    events = [e for e in events if e.get("event_type", "solo") in ("solo", "pair")
              and profile_name in (e.get("profile_name"), e.get("profile_1_name"), e.get("profile_2_name"))]
    return dedupe_titles([t for e in events[-8:] for t in e.get("recommended_titles", []) if isinstance(t, str)])


defaults = {
    "app_mode": "solo", "solo_reroll": 0, "pair_reroll": 0,
    "solo_selected_profile": "", "solo_active_profile": "", "solo_movies_input": "", "solo_output": "",
    "solo_suggested_titles": {}, "solo_skipped_titles": {},
    "pair_mode_type": "Enter manually", "pair_profile_1": "", "pair_profile_2": "",
    "pair_name_1": "You", "pair_name_2": "Friend", "pair_movies_1": "", "pair_movies_2": "",
    "pair_output": "", "pair_suggested_titles": [], "example_result": False,
    "editor_selected_profile": "", "editor_profile_name": "", "editor_profile_movies": "",
    "profile_msg": "", "profile_msg_type": "", "tonight_notes": "",
}
for key, value in defaults.items():
    # Reassign existing values so Streamlit keeps inputs when their mode is hidden.
    st.session_state[key] = st.session_state.get(key, value)


def reset_solo_results():
    st.session_state.solo_output = ""
    st.session_state.solo_active_profile = ""
    st.session_state.solo_reroll = 0


def reset_pair_results():
    st.session_state.pair_output = ""
    st.session_state.pair_selection = None
    st.session_state.pair_reroll = 0
    st.session_state.example_result = False


def reset_results():
    reset_solo_results()
    reset_pair_results()


def set_mode_solo():
    st.session_state.app_mode = "solo"


def set_mode_with_someone():
    st.session_state.app_mode = "with_someone"


def on_solo_profile_change():
    reset_solo_results()
    profile = load_profiles().get(st.session_state.solo_selected_profile, {})
    st.session_state.solo_movies_input = "\n".join(profile.get("favorites", []))


def on_pair_profile_change(number):
    reset_pair_results()
    name = st.session_state[f"pair_profile_{number}"]
    profile = load_profiles().get(name, {})
    st.session_state[f"pair_name_{number}"] = name or ("You" if number == 1 else "Friend")
    st.session_state[f"pair_movies_{number}"] = "\n".join(profile.get("favorites", []))


def on_editor_profile_change():
    st.session_state.confirm_delete = False
    name = st.session_state.editor_selected_profile
    profile = load_profiles().get(name, {})
    st.session_state.editor_profile_name = name
    st.session_state.editor_profile_movies = "\n".join(profile.get("favorites", []))


def save_profile_callback():
    st.session_state.confirm_delete = False
    name = st.session_state.editor_profile_name.strip()
    movies = parse_movie_input(st.session_state.editor_profile_movies)
    if not name or not movies:
        st.session_state.profile_msg = "Enter a profile name and at least one favourite movie."
        st.session_state.profile_msg_type = "warning"
        return
    selected = st.session_state.get("editor_selected_profile", "")
    existing_names = load_profiles()
    if selected and name != selected:
        st.session_state.profile_msg = "A saved profile’s name stays the same. Choose a blank profile to create another."
        st.session_state.profile_msg_type = "warning"
        return
    if not selected and name in existing_names:
        st.session_state.profile_msg = "That profile already exists. Load it from the list to update its favourites."
        st.session_state.profile_msg_type = "warning"
        return
    if selected and selected not in existing_names:
        st.session_state.profile_msg = "That profile is no longer available. Reload the page before saving."
        st.session_state.profile_msg_type = "warning"
        return
    try:
        if not selected:
            supabase.table("profiles").insert({"profile_name": name}).execute()
    except Exception:
        st.session_state.profile_msg = "The profile could not be saved. Please try again."
        st.session_state.profile_msg_type = "warning"
        return
    if not record_feedback(name, "replace_favorites", titles=movies):
        return
    reset_results()
    st.session_state.editor_selected_profile = name
    st.session_state.solo_selected_profile = name
    st.session_state.solo_movies_input = "\n".join(movies)
    for number in (1, 2):
        if st.session_state[f"pair_profile_{number}"] == name:
            st.session_state[f"pair_movies_{number}"] = "\n".join(movies)
    st.session_state.profile_msg = f"Saved profile: {name}"
    st.session_state.profile_msg_type = "success"


def delete_profile_callback():
    name = st.session_state.editor_selected_profile
    if not name or not st.session_state.get("confirm_delete"):
        return
    try:
        for field in ("profile_name", "profile_1_name", "profile_2_name"):
            supabase.table("recommendation_events").delete().eq(field, name).execute()
        supabase.table("movie_feedback").delete().eq("profile_name", name).execute()
        supabase.table("profiles").delete().eq("profile_name", name).execute()
    except Exception:
        st.session_state.profile_msg = "The profile could not be fully deleted. Please try again."
        st.session_state.profile_msg_type = "warning"
        return
    reset_results()
    for key in ("editor_selected_profile", "editor_profile_name", "editor_profile_movies"):
        st.session_state[key] = ""
    if st.session_state.get("solo_selected_profile") == name:
        st.session_state.solo_selected_profile = ""
        st.session_state.solo_movies_input = ""
    st.session_state.solo_suggested_titles.pop(name, None)
    st.session_state.solo_skipped_titles.pop(name, None)
    for number in (1, 2):
        if st.session_state[f"pair_profile_{number}"] == name:
            st.session_state[f"pair_profile_{number}"] = ""
            st.session_state[f"pair_movies_{number}"] = ""
    st.session_state.confirm_delete = False
    st.session_state.profile_msg = f"Deleted profile: {name}"
    st.session_state.profile_msg_type = "success"


def get_solo_response(profile_name, movies, profile, progress):
    history = st.session_state.solo_suggested_titles.get(profile_name, [])
    skipped = st.session_state.solo_skipped_titles.get(profile_name, [])
    progress(f"Finding three films from all {len(movies)} favourites…")
    try:
        selection = select_solo(
            call_openai_text, movies, memory=profile, tonight=st.session_state.tonight_notes,
            excluded_titles=skipped, recent_titles=dedupe_titles(history + recent_recommended_titles(profile_name)),
        )
    except RecommendationError as error:
        st.warning(str(error))
        return ""
    if not selection:
        return ""
    titles = [pick["title"] for pick in selection["picks"]]
    st.session_state.solo_suggested_titles[profile_name] = dedupe_titles(history + titles)
    output = json.dumps(selection, ensure_ascii=False)
    if profile_name:
        create_recommendation_event(profile_name, movies, output, titles)
    return output


def get_pair_response(names, favourites, profiles, use_profiles):
    history = st.session_state.pair_suggested_titles
    memories = [profiles.get(name, empty_profile_record()) if use_profiles else empty_profile_record() for name in names]
    excluded = history + favourites[0] + favourites[1]
    for name, memory in zip(names, memories):
        excluded += memory.get("seen_movies", []) + memory.get("rejected_movies", [])
        if use_profiles:
            excluded += recent_recommended_titles(name)
    try:
        selection = select_pair(call_openai_text, [], names, favourites,
                                {"mood": st.session_state.tonight_notes or "Anything"}, dedupe_titles(excluded), memories)
    except RecommendationError as error:
        st.warning(str(error))
        return ""
    if not selection:
        return ""
    titles = [pick["title"] for pick in selection["picks"].values()]
    st.session_state.pair_suggested_titles = dedupe_titles(history + titles)
    st.session_state.pair_selection = selection
    st.session_state.pair_result_mode = "ai_only"
    output = to_display_text(selection, names)
    if use_profiles:
        create_recommendation_event(" + ".join(names), favourites[0] + favourites[1], output, titles,
                                    event_type="pair", names=names,
                                    best_pick=selection["picks"][selection["best_slot"]]["title"])
    return output


def render_recommendation_card(label, title, reason, metadata=None, primary=False,
                               viewer_reasons=None, tradeoff=None, **unused):
    if label:
        st.markdown(f"### {label}")
    with st.container(border=True):
        if primary:
            st.caption("OUR PICK FOR TONIGHT")
        st.markdown(f"**{title}**")
        if reason:
            st.write(reason)
        for name, explanation in viewer_reasons or []:
            st.caption(f"WHY IT FITS {name.upper()}")
            st.write(explanation)
        if tradeoff:
            st.caption("POSSIBLE DRAWBACK")
            st.write(tradeoff)


def feedback_button(label, profile_name, title, action, key):
    if st.button(label, key=key, use_container_width=True):
        if record_feedback(profile_name, action, title):
            if action in ("like", "dislike"):
                st.session_state.profile_refresh = {
                    "name": profile_name,
                    "previous_favorites": profiles.get(profile_name, {}).get("favorites", []),
                }
            st.session_state.feedback_notice = f"Saved: {title}"
            st.rerun()


def render_solo_feedback(profile_name, title, index):
    cols = st.columns(3)
    with cols[0]:
        if profile_name:
            feedback_button("Save for later", profile_name, title, "save", f"solo_save_{index}")
    with cols[1]:
        if st.button("Not tonight", key=f"solo_skip_{index}", use_container_width=True):
            skipped = st.session_state.solo_skipped_titles.setdefault(profile_name, [])
            st.session_state.solo_skipped_titles[profile_name] = dedupe_titles(skipped + [title])
            st.rerun()
    with cols[2]:
        if profile_name:
            feedback_button("Already seen", profile_name, title, "seen", f"solo_seen_{index}")
    if title_key(title) in {title_key(t) for t in st.session_state.solo_skipped_titles.get(profile_name, [])}:
        st.caption("Skipped for this session. This won’t count as a dislike.")
    if profile_name:
        with st.expander("Watched it? Tell Tastebud what you thought"):
            left, right = st.columns(2)
            with left:
                feedback_button("Liked it", profile_name, title, "like", f"solo_like_{index}")
            with right:
                feedback_button("Didn’t like it", profile_name, title, "dislike", f"solo_dislike_{index}")


def render_solo_results(output, profiles):
    selection = json.loads(output)  # Only validated selections enter session state.
    profile_name = st.session_state.solo_active_profile
    st.markdown("## Three films for tonight")
    st.caption("Film details are generated and may need checking.")
    for index, pick in enumerate(selection["picks"]):
        render_recommendation_card("", pick["title"], pick["reason"], primary=index == 0)
        st.caption(f"A connection to your taste: {pick['anchor']}")
        render_solo_feedback(profile_name, pick["title"], index)
    with st.expander("What Tastebud picked up from your favourites"):
        st.write(selection["taste_read"])
        st.caption("A working interpretation of your films, not a measured taste score.")
    if not profile_name:
        st.caption("Save a profile below to keep a watchlist and record what you enjoyed. Guest profiles last for this session.")


def render_film_library(name, profile):
    with st.expander("Your watchlist and watched films"):
        with st.form(f"record_watched_{name}", clear_on_submit=True):
            watched_title = st.text_input("Record a film you watched", placeholder="Title (year), if you know it")
            reaction = st.selectbox("What did you think?", ["Just record watched", "Liked it", "Didn’t like it"])
            if st.form_submit_button("Record film"):
                if not watched_title.strip():
                    st.warning("Enter the film’s title first.")
                else:
                    action = {"Just record watched": "seen", "Liked it": "like", "Didn’t like it": "dislike"}[reaction]
                    if record_feedback(name, action, clean_title(watched_title)):
                        st.session_state.profile_refresh = {"name": name, "previous_favorites": profile["favorites"]}
                        st.session_state.feedback_notice = f"Recorded: {watched_title.strip()}"
                        st.rerun()
        watchlist_tab, watched_tab = st.tabs(["Want to watch", "Watched"])
        with watchlist_tab:
            items = profile.get("watchlist", [])
            if not items:
                st.caption("Save a recommendation and it will appear here.")
            show_all = len(items) > 20 and st.checkbox("Show all saved films", key=f"show_watchlist_{name}")
            for item in (items if show_all else items[:20]):
                title = item["movie_title"]
                st.markdown(f"**{title}**")
                left, right = st.columns(2)
                with left:
                    feedback_button("Mark watched", name, title, "seen", f"watchlist_watched_{name}_{title}")
                with right:
                    feedback_button("Remove", name, title, "remove_saved", f"watchlist_remove_{name}_{title}")
        with watched_tab:
            seen = profile.get("seen_movies", [])
            if not seen:
                st.caption("Mark a film as watched, then tell Tastebud whether you liked it.")
            search = st.text_input("Find a watched film", key=f"watched_search_{name}") if len(seen) > 20 else ""
            if search.strip():
                seen = [title for title in seen if search.casefold().strip() in title.casefold()]
            liked = {title_key(t) for t in profile.get("favorites", [])}
            disliked = {title_key(t) for t in profile.get("disliked_movies", [])}
            st.caption("Your 20 most recently recorded watched films. Liking a film adds it to your favourites.")
            for title in seen[:20]:
                st.markdown(f"**{title}**")
                if title_key(title) in liked:
                    st.caption("You liked this")
                elif title_key(title) in disliked:
                    st.caption("You didn’t like this")
                left, right = st.columns(2)
                with left:
                    feedback_button("Liked it", name, title, "like", f"watched_like_{name}_{title}")
                with right:
                    feedback_button("Didn’t like it", name, title, "dislike", f"watched_dislike_{name}_{title}")


def render_pair_results():
    if st.session_state.example_result:
        st.info("Example result · this is a sample, not a recommendation based on your films.")
        st.markdown("## 🎯 This is the one")
        render_recommendation_card("", "Knives Out", "A lively mystery with enough wit to keep the evening light.", primary=True)
        with st.expander("Two alternatives"):
            render_recommendation_card("", "The Grand Budapest Hotel", "An intricate caper with a warm heart.")
            render_recommendation_card("", "Hunt for the Wilderpeople", "An offbeat adventure built around an unlikely friendship.")
        return

    render_selection(st.session_state.pair_selection,
                     [st.session_state.pair_name_1 or "You", st.session_state.pair_name_2 or "Friend"],
                     {}, render_recommendation_card, list(profiles), update_profile_feedback, "ai_only")


st.caption("TASTEBUD · LESS SCROLLING, MORE WATCHING")
st.title("Find your next good film.")
st.write("A few thoughtful picks for you, or a film you’ll both look forward to.")
left, right = st.columns(2)
with left:
    st.button("For me", key="nav_solo", on_click=set_mode_solo, use_container_width=True)
with right:
    st.button("With someone", key="nav_pair", on_click=set_mode_with_someone, use_container_width=True)

profiles = load_profiles()
profile_names = list(profiles)
refresh = st.session_state.pop("profile_refresh", None)
if refresh and refresh["name"] in profiles:
    updated_movies = "\n".join(profiles[refresh["name"]]["favorites"])
    # Refresh saved-profile mirrors while preserving any manually edited draft.
    for selector, movie_input in (("solo_selected_profile", "solo_movies_input"),
                                   ("editor_selected_profile", "editor_profile_movies"),
                                   ("pair_profile_1", "pair_movies_1"),
                                   ("pair_profile_2", "pair_movies_2")):
        if (st.session_state.get(selector) == refresh["name"]
                and parse_movie_input(st.session_state.get(movie_input, "")) == refresh["previous_favorites"]):
            st.session_state[movie_input] = updated_movies
if st.session_state.get("feedback_notice"):
    st.success(st.session_state.pop("feedback_notice"))
with st.expander("Tonight’s mood (optional)"):
    st.text_input("What would feel good tonight?", key="tonight_notes", max_chars=500,
                  placeholder="Something clever and funny. Nothing too bleak.", on_change=reset_results)
    st.caption("This guides tonight’s picks; it doesn’t change your saved taste.")
    if st.button("Start a fresh movie session", key="reset_pair_history"):
        st.session_state.solo_suggested_titles = {}
        st.session_state.solo_skipped_titles = {}
        st.session_state.pair_suggested_titles = []
        reset_results()
        st.rerun()

if st.session_state.app_mode == "solo":
    st.markdown("## What do you love watching?")
    st.selectbox("Load a saved profile (optional)", [""] + profile_names, key="solo_selected_profile", on_change=on_solo_profile_change)
    st.text_area("Movies you love", height=180, key="solo_movies_input", on_change=reset_solo_results,
                 placeholder="One film per line. Your full list is welcome.")
    movies = parse_movie_input(st.session_state.solo_movies_input)
    name = st.session_state.solo_selected_profile
    profile = profiles.get(name, empty_profile_record())
    left, right = st.columns(2)
    with left:
        find_clicked = st.button("Find me a film", key="solo_find", use_container_width=True, type="primary")
    with right:
        reroll_clicked = st.button("Try three more", key="solo_reroll_button", use_container_width=True,
                                   disabled=not st.session_state.solo_output)
    if find_clicked or reroll_clicked:
        if not movies:
            st.warning("Enter at least one movie you love.")
        else:
            st.session_state.solo_reroll = st.session_state.solo_reroll + 1 if reroll_clicked else 0
            st.session_state.solo_active_profile = name
            with st.status("Finding your films…", expanded=True) as status:
                st.session_state.solo_output = get_solo_response(name, movies, profile, lambda text: status.update(label=text))
                status.update(label="Your three picks are ready" if st.session_state.solo_output else "Couldn’t finish this round",
                              state="complete" if st.session_state.solo_output else "error", expanded=False)
            if st.session_state.solo_output:
                st.rerun()
    if st.session_state.solo_output:
        st.status("Your three picks are ready", state="complete", expanded=False)
        render_solo_results(st.session_state.solo_output, profiles)
    if name:
        render_film_library(name, profile)

else:
    st.markdown("## Who’s watching tonight?")
    st.caption("Start with a few favourites each. One film per line; longer lists are welcome.")
    if len(profile_names) >= 2:
        pair_mode = st.radio("Start with", ["Enter manually", "Use saved profiles"], horizontal=True,
                             key="pair_mode_type", on_change=reset_pair_results)
    else:
        pair_mode = "Enter manually"
    if st.button("See an example", key="show_example"):
        st.session_state.pair_output = "example"
        st.session_state.example_result = True
    use_profiles = pair_mode == "Use saved profiles"
    if use_profiles:
        for number in (1, 2):
            if st.session_state[f"pair_profile_{number}"] not in profile_names:
                st.session_state[f"pair_profile_{number}"] = profile_names[number - 1]
                reset_pair_results()
            selected_name = st.session_state[f"pair_profile_{number}"]
            st.session_state[f"pair_name_{number}"] = selected_name
            st.session_state[f"pair_movies_{number}"] = "\n".join(profiles[selected_name]["favorites"])
        left, right = st.columns(2)
        for number, col in enumerate((left, right), 1):
            with col:
                st.selectbox(f"Person {number}", profile_names, key=f"pair_profile_{number}",
                             on_change=on_pair_profile_change, args=(number,))
                st.caption(f"{len(parse_movie_input(st.session_state[f'pair_movies_{number}']))} favourites loaded")
                with st.expander("View favourites"):
                    st.write(st.session_state[f"pair_movies_{number}"])
    else:
        left, right = st.columns(2)
        with left:
            st.text_input("Your name (optional)", key="pair_name_1", max_chars=60, on_change=reset_pair_results)
            st.text_area("Your favourite films", key="pair_movies_1", height=130, on_change=reset_pair_results,
                         placeholder="Arrival\nKnives Out\nThe Martian")
        with right:
            st.text_input("Their name (optional)", key="pair_name_2", max_chars=60, on_change=reset_pair_results)
            st.text_area("Their favourite films", key="pair_movies_2", height=130, on_change=reset_pair_results,
                         placeholder="Paddington 2\nLittle Miss Sunshine\nAmélie")
    suffix = "profiles" if use_profiles else "manual"
    left, right = st.columns(2)
    with left:
        find_clicked = st.button("Find our movie", key=f"pair_match_{suffix}", use_container_width=True, type="primary")
    with right:
        reroll_clicked = st.button("Try another pick", key=f"pair_reroll_{suffix}", use_container_width=True,
                                   disabled=not st.session_state.pair_output or st.session_state.example_result)
    if find_clicked or reroll_clicked:
        favourites = [parse_movie_input(st.session_state[f"pair_movies_{n}"]) for n in (1, 2)]
        if not all(favourites):
            st.warning("Please enter at least one movie for both people.")
        elif use_profiles and st.session_state.pair_profile_1 == st.session_state.pair_profile_2:
            st.warning("Choose two different profiles for shared picks.")
        else:
            st.session_state.pair_reroll = st.session_state.pair_reroll + 1 if reroll_clicked else 0
            st.session_state.example_result = False
            names = [st.session_state.pair_name_1 or "You", st.session_state.pair_name_2 or "Friend"]
            with st.spinner("Finding the overlap between your favourites…"):
                st.session_state.pair_output = get_pair_response(names, favourites, profiles, use_profiles)
            if st.session_state.pair_output:
                st.rerun()
    if st.session_state.pair_output:
        render_pair_results()

st.markdown("---")
with st.expander("Create or update profile"):
    st.selectbox("Load existing profile to edit (optional)", [""] + profile_names,
                 key="editor_selected_profile", on_change=on_editor_profile_change)
    st.text_input("Profile name", key="editor_profile_name", max_chars=60,
                  disabled=bool(st.session_state.editor_selected_profile))
    if st.session_state.editor_selected_profile:
        st.caption("To create another profile, choose the blank option above.")
    st.text_area("Favourite movies (one per line)", key="editor_profile_movies", height=150)
    st.button("Save profile", key="save_profile_button", on_click=save_profile_callback)
    if st.session_state.profile_msg:
        (st.success if st.session_state.profile_msg_type == "success" else st.warning)(st.session_state.profile_msg)
        st.session_state.profile_msg = ""
    if st.session_state.editor_selected_profile:
        st.checkbox("I want to delete this profile and its history", key="confirm_delete")
        st.button("Delete profile", key="delete_profile_button", on_click=delete_profile_callback,
                   disabled=not st.session_state.get("confirm_delete"))
