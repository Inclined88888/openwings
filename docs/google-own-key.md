# Your own Google key: Sign in with Google for everyone on your OpenWings

**Who this is for:** the admin of an OpenWings install. You do this once; after
that, everyone on your OpenWings gets a real "Sign in with Google" button for
Gmail, their main Google calendar (with Meet links) and Google Drive.

**Why it is needed:** Google charges for the yearly security review that a
shared, one-click Gmail sign-in would need, and its rules do not allow an open
source project to hand out one key for everyone. So each OpenWings uses its own
free Google key, made by its admin. Home Assistant, n8n, Nextcloud and rclone
work the same way. It is free and takes about 10 minutes.

**Without it**, people can still use Gmail with an app password and add any
Google calendar by link (read only). See the README, section
[Connect your accounts](../README.md#accounts).

The same steps are in the app: Settings > Integrations > Connect your accounts >
Google Calendar and Drive > **Set up (admin, once)**. The app also shows the
exact redirect address to paste, with a Copy button.

## The steps

Sign in to the Google Cloud console with any Google account you control (it
does not have to be the account people will connect). Each link opens the right
page of the console.

1. **Create a project.** <https://console.cloud.google.com/projectcreate>.
   Any name, for example OpenWings. Make sure the new project is selected in the
   bar at the top of the console for the next steps.
2. **Turn on the APIs.** Open each link and press **Enable**:
   * Gmail API: <https://console.cloud.google.com/apis/enableflow;apiid=gmail.googleapis.com>
   * Google Calendar API: <https://console.cloud.google.com/apis/enableflow;apiid=calendar-json.googleapis.com>
   * CalDAV API: <https://console.cloud.google.com/apis/enableflow;apiid=caldav.googleapis.com>
   * Google Drive API: <https://console.cloud.google.com/apis/enableflow;apiid=drive.googleapis.com>
3. **Set up the sign-in screen.** <https://console.cloud.google.com/auth/branding>.
   Press **Get started**. App name: OpenWings. User support email: yours.
   Audience: **External**. Contact email: yours. Agree to the Google API
   Services User Data Policy, then **Create**.
4. **Publish it.** <https://console.cloud.google.com/auth/audience>. Under
   Publishing status press **Publish app**. While it says Testing, only the
   people you list there as test users can sign in, and only for 7 days at a
   time. If the Publish button is greyed out, Google wants a home page and a
   privacy policy link first: add them under Branding (your own web page, or
   this OpenWings address). After you publish, the console may say the app
   needs verification (Gmail and Drive are "restricted" permissions): you can
   ignore that for your own OpenWings. Unverified, up to 100 Google accounts
   can sign in, and nothing has to be paid.
5. **Make the key.** <https://console.cloud.google.com/auth/clients>. Press
   **Create client**. Application type: **Web application**. Name: OpenWings.
   If the form shows **Use this client for an AI-powered agent**, leave it
   unticked (it cannot be changed later).
   Under **Authorized redirect URIs** press **Add URI** and paste the address
   the OpenWings wizard shows, for example
   `http://localhost:7011/api/email/oauth/google/callback`. Press **Create**.
6. **Paste the key into OpenWings.** Copy the **Client ID** and the **Client
   secret** from the dialog Google shows (Google shows the secret only once) and
   paste both into the wizard, then **Save**.

A new key usually works within minutes; Google says it can take up to five
hours.

## The redirect address

Google accepts only these redirect addresses (Google's OAuth documentation,
"Redirect URI validation rules"):

* `https://` addresses on a real, public domain name (its ending must be on the
  public suffix list: not `.local`, `.lan`, `.home`, `.internal`), and no bare
  IP address, or
* `http://localhost:<port>/...` or `http://127.0.0.1:<port>/...`.

Plain `http://` on a LAN address such as `http://192.168.1.20:7011` or a
`.local` name is refused by Google. Open OpenWings at `http://localhost:7011` on
the computer it runs on (or through an SSH tunnel), or give it an https address
(`setup.sh --domain`), before you open the wizard: the wizard shows the address
it will use and warns when Google would refuse it. When you open the wizard from
a LAN address, OpenWings uses the localhost address and says so: Sign in with
Google then works only in a browser on the OpenWings computer itself; for
phones and other computers, give OpenWings an https address.

If you later move OpenWings to another address (for example an https domain),
the wizard shows the new address next to the saved one with **Use this address
instead**. Press it, then add the new address under Authorized redirect URIs in
Google's console too.

You only register one address. People who open OpenWings at `127.0.0.1` while
the key was made from `localhost` (or the other way round) are fine: when Google
sends them back to the registered address, OpenWings sends their browser on to
the address they started from, so they stay signed in.

## What people see

When a person presses **Sign in with Google**, they tick what OpenWings may use:

| Tick | Google permission | What OpenWings does with it |
| --- | --- | --- |
| Mail | `https://mail.google.com/` (the one Gmail's IMAP and SMTP need) | Their Gmail in Email; the assistant's mail tools (sending still needs their click) |
| Calendar | `https://www.googleapis.com/auth/calendar` | Two way sync of their main Google calendar; "Add a Google Meet link" on new events. Other Google calendars can be added read only with Calendar by link |
| Drive | `https://www.googleapis.com/auth/drive.readonly` | The assistant can search and read their files (read only, 10 MB a file) |

Google then shows its own page. Because the key is yours and not reviewed by
Google, Google first says it **has not verified this app**: choose **Advanced**,
then continue. Google then shows a box for each permission: leave them all
ticked. Back in OpenWings, a note says what was connected, and which part Google
did not allow if a box was unticked. That warning is normal for a private key. An unreviewed key can
be used by up to 100 Google accounts, which is plenty for one household or
team.

What a person connects is used only for that person: other people on your
OpenWings, admins included, cannot use their mail, calendar or files.

## Already using `.env`?

If you set `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET` and
`GOOGLE_OAUTH_REDIRECT_URI` in `.env` before, nothing changes: the wizard shows
"Set in .env" and every sign-in uses those values. A key saved in the wizard
takes priority over `.env`. Make sure the Google client also has the Calendar,
CalDAV and Drive APIs turned on (step 2) if you want those.

## Where it is stored

The Client ID and secret are stored in OpenWings' database, encrypted with the
key in `.env` (`OPENWINGS_APP_KEY`), like every other secret. The secret is never
shown again or sent to a browser; the wizard shows only the end of the Client
ID. Each person's Google sign-in (access and refresh tokens) is stored the same
way. **Remove** in the wizard deletes the saved key; each person can
**Disconnect** their Google account, which signs OpenWings out at Google.

## Business accounts (Google Workspace)

A Workspace admin can instead create the key inside the organization (Audience:
Internal, no "unverified app" warning and no user limit), or mark your key as
trusted in the Workspace admin console. Some organizations block keys that are
not reviewed; then use app passwords (if allowed) or calendar links.
