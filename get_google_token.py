"""One-time helper: mint a Google Calendar OAuth refresh token as yourself.

Run this LOCALLY (not on Replit) once:

    pip install google-auth-oauthlib
    GOOGLE_OAUTH_CLIENT_ID=... GOOGLE_OAUTH_CLIENT_SECRET=... python get_google_token.py

A browser window opens for you to sign in as sean.hu@rho.co and approve calendar
access. It prints a refresh token — copy it into the bot's GOOGLE_OAUTH_REFRESH_TOKEN
secret (along with the same client id/secret). You only do this once.
"""
import os
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/calendar",
          "https://www.googleapis.com/auth/drive.readonly"]

client_id = os.environ["GOOGLE_OAUTH_CLIENT_ID"]
client_secret = os.environ["GOOGLE_OAUTH_CLIENT_SECRET"]

config = {
    "installed": {
        "client_id": client_id,
        "client_secret": client_secret,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }
}

flow = InstalledAppFlow.from_client_config(config, SCOPES)
creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")

print("\n=== Copy this into the GOOGLE_OAUTH_REFRESH_TOKEN secret ===")
print(creds.refresh_token)
