"""One-time helper: mint a Google OAuth refresh token as yourself (Calendar + Drive).

Run it anywhere (Mac terminal or the Replit shell):

    pip install google-auth-oauthlib
    GOOGLE_OAUTH_CLIENT_ID=... GOOGLE_OAUTH_CLIENT_SECRET=... python get_google_token.py

It prints a URL. Open it, sign in as sean.hu@rho.co, and approve calendar + drive access.
Your browser then lands on a "localhost refused to connect" page — THAT IS EXPECTED.
Copy the full URL from the address bar and paste it back here; the script pulls the code
out of it and prints a refresh token. Put that in GOOGLE_OAUTH_REFRESH_TOKEN.
"""
import os
import urllib.parse
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/calendar",
          "https://www.googleapis.com/auth/drive.readonly"]

config = {
    "installed": {
        "client_id": os.environ["GOOGLE_OAUTH_CLIENT_ID"],
        "client_secret": os.environ["GOOGLE_OAUTH_CLIENT_SECRET"],
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }
}

flow = InstalledAppFlow.from_client_config(config, SCOPES, redirect_uri="http://localhost")
auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent")

print("\n1) Open this URL, sign in as sean.hu@rho.co, and approve:\n")
print(auth_url)
print("\n2) Your browser will show 'localhost refused to connect' — that's expected.")
print("   Copy the FULL URL from the address bar and paste it below.\n")

resp = input("Pasted URL (or just the code): ").strip()
query = urllib.parse.urlparse(resp).query
code = urllib.parse.parse_qs(query).get("code", [resp])[0]

flow.fetch_token(code=code)
print("\n=== Copy this into the GOOGLE_OAUTH_REFRESH_TOKEN secret ===")
print(flow.credentials.refresh_token)
