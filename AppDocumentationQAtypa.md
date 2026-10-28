# App Documentation
> ### This is documentation about app's backend properties

### Ask/AI
From v3.0 _(previously labeled as v2.1)_, app has an "Ask" feature where user can ask AI/algorithm about theirs listening data or library related questions

This feature is inspired by Spotify's feature

> ### Difference between both "Ask" options:
> - **Algorithmic**: algorithm on-device that could pre-embedded questions with user's context data
> - **AI**: cloud/on-device AI model that answers library related questions, or what algorithmic couldn't process

Examples of **algorithm proccessable questions**:
- **_My first track_**: answers about user's first ever on-record listened track
- **_My top tracks this month_**: answers about user's top 10 tracks listened by plays count of current month
- **_My most recent listen_**: answers about user's last on-record listen (10 sec and above are only included)

Examples of **AI questions**:
- **_What that song that goes like "Put your hands to the constellations, ..."_**: would answer with track _(in this case "Devil In A New Dress" by ye & rick ross)_
- **_What song that has collab with Yeat and Don Toliver?_**: would answer with track _(in this case "Rendezvous" on OCTANE)_

AI questions are answered by these models: openai-fast _(Pollinations Classic)_, openai/gpt-oss-120b _(Hugging Face; requires API token)_; API gates and everything is took from static document `https://yzyworks.com/WholeLib/AskProviders.dict` _(request for this document is only when user agrees to use Cloud models provided)_
