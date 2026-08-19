"""The local job board: a stdlib HTTP server on 127.0.0.1 with a session token.

Split out of the single-file `tools/jobs_board.py` in milestone M1 of
`.duet/jobflow-ui/DESIGN.md`. `tools/jobs_board.py` stays as the entry point
people already type; everything it used to hold lives here:

    server.py    routing, token auth, static files, the fetch endpoints
    state.py     seen_jobs.json read/write and the board payload
    activity.py  the structured event log behind the activity strip
    static/      the page itself, as files on disk rather than a constant

Serving the page from disk is what retired the old `STALE_BANNER`: editing
`static/app.js` and refreshing now does what it looks like it does.
"""
