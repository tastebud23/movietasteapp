"""Optional Supabase email authentication; clients belong to one session only."""
import streamlit as st
import base64
import json
from urllib.parse import urlparse
from supabase import create_client
from storage import AccountStore, GuestStore


def is_public_key(key):
    if key.startswith("sb_publishable_"):
        return True
    try:
        payload = key.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["role"] == "anon"
    except (ValueError, IndexError, KeyError, TypeError):
        return False


def storage_for_session(url, public_key):
    # Never use service-role/secret keys here: auth must run with user permissions.
    with st.sidebar:
        st.subheader("Your movie space")
        client = st.session_state.get("account_client")
        owner_id = st.session_state.get("account_owner")
        if client and owner_id:
            st.caption("Signed in · your saved profiles are private to your account.")
            if st.button("Sign out"):
                try:
                    client.auth.sign_out()
                finally:
                    st.session_state.clear()
                    st.rerun()
            return AccountStore(client, owner_id)

        st.caption("Guest mode · profiles stay in this browser session only.")
        configured = url and public_key
        if configured and (urlparse(url).scheme != "https" or not is_public_key(public_key)):
            st.warning("Account sign-in is unavailable. Configure an HTTPS Supabase URL and a publishable or anon key.")
            configured = False
        if configured:
            with st.expander("Sign in to keep your profiles"):
                st.caption("Guest profiles are not automatically transferred when you sign in.")
                with st.form("account_form"):
                    email = st.text_input("Email", autocomplete="email")
                    password = st.text_input("Password", type="password")
                    action = st.radio("Account action", ["Sign in", "Create account"])
                    submitted = st.form_submit_button("Continue")
                if submitted:
                    if not email.strip() or not password:
                        st.warning("Enter your email and password.")
                    else:
                        try:
                            client = create_client(url, public_key)
                            credentials = {"email": email.strip(), "password": password}
                            if action == "Create account":
                                result = client.auth.sign_up(credentials)
                            else:
                                result = client.auth.sign_in_with_password(credentials)
                            if result.session and result.user:
                                st.session_state.clear()
                                st.session_state.account_client = client
                                st.session_state.account_owner = str(result.user.id)
                                st.rerun()
                            else:
                                st.success("Check your email to confirm your account, then sign in.")
                        except Exception:
                            st.error("Could not complete sign-in. Check your details and try again.")
        else:
            st.caption("You can try the app without creating an account.")
    tables = st.session_state.setdefault("guest_tables", {})
    return GuestStore(tables)
