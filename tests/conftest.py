import os

# LinkedIn requests are paced with real sleeps (tools/collectors.py); the suite
# fakes every subprocess, so pacing would only make it slow.
os.environ.setdefault("LINKEDIN_PACE", "0")
