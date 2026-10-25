"""Rendering for validated shared picks; no parsing of model prose."""
import streamlit as st
from pair_recommendations import title_key


def render_selection(selection, names, metadata, card, profiles, save, mode):
    if mode == 'catalogue':
        st.caption('Chosen from checked movie entries. Availability can change; confirm with your provider.')
    else:
        st.info('AI-only suggestions: movie details, age suitability and streaming availability have not been verified.')
    best = selection['best_slot']
    def show(slot, primary=False):
        pick = selection['picks'][slot]
        card('', pick['title'], '', metadata=metadata.get(title_key(pick['title']), {}), primary=primary,
             viewer_reasons=list(zip(names, [pick['for_one'], pick['for_two']])), tradeoff=pick['tradeoff'])
    st.markdown('## 🎯 This is the one')
    show(best, primary=True)
    with st.expander('Two alternatives'):
        for slot, label in [('safe', 'Easy win'), ('compromise', 'Middle ground'), ('wildcard', 'Curveball')]:
            if slot != best:
                st.markdown('### ' + label)
                show(slot)
    with st.expander('Why this works for both of you'):
        st.write(selection['summary'])
        st.markdown('**Where you align**')
        for text in selection['align']:
            st.write('• ' + text)
        st.markdown('**Where you differ**')
        for text in selection['differ']:
            st.write('• ' + text)
    if profiles:
        destination = st.selectbox('Save tonight’s pick to a watchlist', profiles, key='pair_save_profile')
        if st.button('Save this film', key='save_pair_pick'):
            save(destination, selection['picks'][best]['title'], 'saved_movies', 'pair_best_pick')
            st.success(f'Saved to {destination}’s watchlist.')
    else:
        st.caption('Create a profile below to keep this pick for this session.')
