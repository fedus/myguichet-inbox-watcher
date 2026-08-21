# MyGuichet inbox watcher

This small, unofficial tool checks a MyGuichet inbox, downloads attachments
from messages it has not processed before, and saves them locally. It is a
foundation for later integrations such as Paperless-ngx or email forwarding.

It uses the normal MyGuichet/LuxTrust login flow: the username and password
can be entered by Playwright, but the LuxTrust mobile/device approval is
always required. It does not bypass MFA.

It is designed for a private macOS or Linux machine with Python 3.10+. It can
watch one MyGuichet login or several separately configured logins.

> MyGuichet does not provide this project as an official API client. The portal
> and authentication UI can change, so use it only for your own account and
> keep an eye on its normal terms of use.

## What the files do

| File | Responsibility |
| --- | --- |
| `myguichet_get_new_messages.py` | Main command: refreshes a missing/expired session when needed, finds unprocessed messages, and downloads their attachments. |
| `login_and_grab_cookie.py` | Opens the normal portal in Chromium, performs first-factor LuxTrust login, waits for device approval, and writes `cookie.txt`. |
| `mqtt_trigger.py` | Long-running MQTT subscriber that triggers one-account or all-account polling. |
| `client.py` | Read-only HTTP client for the MyGuichet endpoints. |
| `config.py` | Shared `.env` settings and validation. |
| `storage.py` | Private atomic file writes and a lock that prevents overlapping runs. |

Runtime data is deliberately kept out of Git:

- `.env` — LuxTrust credentials and optional configuration.
- `.browser-profile/` — a private Chromium profile; it can contain an active session.
- `cookie.txt` — an active API session credential.
- `state.json` — message IDs already processed.
- `downloads/` — downloaded government documents.
- `accounts/` — per-account runtime files when `MYGUICHET_ACCOUNTS` is used.

## Install

Run all commands from this directory:

```sh
cd /path/to/myguichet-api/inbox_watcher
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
cp .env.example .env
chmod 600 .env
```

On a Linux server, Playwright may also need OS browser libraries. If you have
administrator access, use:

```sh
.venv/bin/python -m playwright install --with-deps chromium
```

Without administrator access, ask the host administrator to install the
dependencies reported by Playwright.

## Configure

For one login, the existing variables still work. Edit `.env`:

```dotenv
LUXTRUST_USERNAME=your-user-id
LUXTRUST_PASSWORD=your-password
MYGUICHET_HEADLESS=true
MYGUICHET_LANGUAGE=fr
MYGUICHET_SPACE_ID=your-space-id
MYGUICHET_DOWNLOAD_DIR=downloads
```

For more than one login, set `MYGUICHET_ACCOUNTS` and define each account with
the account name in the variable. Account names may contain only letters,
numbers, and underscores:

```dotenv
MYGUICHET_ACCOUNTS=alice,bob

MYGUICHET_ALICE_LUXTRUST_USERNAME=alice-user-id
MYGUICHET_ALICE_LUXTRUST_PASSWORD=alice-password
MYGUICHET_ALICE_SPACE_ID=alice-space-id
MYGUICHET_ALICE_DOWNLOAD_DIR=/srv/myguichet/alice

MYGUICHET_BOB_LUXTRUST_USERNAME=bob-user-id
MYGUICHET_BOB_LUXTRUST_PASSWORD=bob-password
MYGUICHET_BOB_SPACE_ID=bob-space-id
MYGUICHET_BOB_DOWNLOAD_DIR=/srv/myguichet/bob
```

`MYGUICHET_HEADLESS=true` is appropriate for a shell-only server. Set it to
`false` temporarily if you need to see the browser while diagnosing a changed
login screen.

The space ID is required and different for every account -- there is no shared
default. To find one, log in once with `MYGUICHET_HEADLESS=false`, open DevTools
> Network, and look at any `/fpgun-iep-api/api/space/v1/...` request URL; the
number in the path is your space ID.

Per-account runtime state is stored in `accounts/<account>/` by default:
`cookie.txt`, `state.json`, `.browser-profile/`, and `.run.lock`. The legacy
single-account setup continues to use the repository root for those files.
Per-account downloads default to `downloads/<account>/` when
`MYGUICHET_ACCOUNTS` is set, but `MYGUICHET_<ACCOUNT>_DOWNLOAD_DIR` can point to
any private absolute path or a path relative to this repository.

Optional safety settings:

```dotenv
# Seconds to wait for the LuxTrust external-device approval (default: 300).
MYGUICHET_LOGIN_TIMEOUT_SECONDS=300

# Reject individual attachments above this size instead of filling memory/disk
# unexpectedly (default: 100 MiB).
MYGUICHET_MAX_ATTACHMENT_MB=100
```

If the username or password is omitted, an interactive run prompts for the
missing value without saving it. A scheduler has no interactive terminal, so
an unattended setup must put both values in `.env`.

## Run once

```sh
.venv/bin/python myguichet_get_new_messages.py
```

With `MYGUICHET_ACCOUNTS` set, that command polls all configured accounts in
sequence. To poll only one account:

```sh
.venv/bin/python myguichet_get_new_messages.py --account alice
```

On the first run without that account's `cookie.txt`, it starts the login flow.
Enter the LuxTrust approval on the matching external device within the
configured timeout. A headless browser has no visible window, but the device
approval still works.

The watcher then:

1. Pages through the inbox until it reaches a section already recorded in
   `state.json`.
2. Downloads attachments to the account's download directory using a stable
   name. When the portal response includes them, filenames are prefixed with
   the message date, sender, and title, followed by communication and document
   IDs for uniqueness.
3. Atomically records each fully completed message in that account's
   `state.json`.

An empty `state.json` means the first run treats all available inbox messages
as new. Existing state from earlier runs prevents reprocessing. Only
attachments are downloaded; the message body and metadata are not exported.
If a process is interrupted after a file download but before its state
checkpoint, that message is safely downloaded again on the next run. The
delivery model is therefore **at least once**, rather than exactly once.

For login troubleshooting only, run the login command directly:

```sh
.venv/bin/python login_and_grab_cookie.py --account alice
```

Do not run that command while the regular watcher is running for the same
account, because both use that account's private Chromium profile. The script
detects that overlap and exits without touching the session.

## MQTT trigger

`mqtt_trigger.py` is a long-running subscriber. It connects to an existing
broker and triggers the same polling code used by the one-shot command.

```dotenv
MYGUICHET_MQTT_HOST=localhost
MYGUICHET_MQTT_PORT=1883
MYGUICHET_MQTT_TOPIC=myguichet/poll
# Optional: publish per-account results such as "alice OK 0".
MYGUICHET_MQTT_STATUS_TOPIC=myguichet/poll/status
```

Run it with:

```sh
.venv/bin/python mqtt_trigger.py
```

Accepted trigger payloads:

- empty payload, `all`, or `*` — poll all configured accounts.
- `alice` — poll one configured account.
- `{"account":"alice"}` — poll one account.
- `{"accounts":["alice","bob"]}` — poll selected accounts.

In Docker, set `MYGUICHET_RUN_MODE=mqtt` to run the MQTT subscriber instead of a
one-shot poll.

## Run regularly

The main command is safe to schedule. It uses a local lock, so a second run
exits cleanly when the previous one is still working. It also performs one
automatic LuxTrust refresh/retry when the saved API session has expired.

### Cron

For a simple check every 15 minutes, edit your user crontab with `crontab -e`:

```cron
*/15 * * * * umask 077; cd /path/to/myguichet-api/inbox_watcher && /path/to/myguichet-api/inbox_watcher/.venv/bin/python myguichet_get_new_messages.py >> /path/to/myguichet-api/inbox_watcher/watcher.log 2>&1
```

Use absolute paths and keep `MYGUICHET_HEADLESS=true` plus both credentials in
`.env`. Review `watcher.log` periodically; it intentionally omits message
subjects and document names to reduce private information in logs.

### systemd user timer (Linux)

For a server, a user timer is usually easier to observe than cron. Create
`~/.config/systemd/user/myguichet-watcher.service`:

```ini
[Unit]
Description=MyGuichet inbox watcher

[Service]
Type=oneshot
WorkingDirectory=/path/to/myguichet-api/inbox_watcher
ExecStart=/path/to/myguichet-api/inbox_watcher/.venv/bin/python myguichet_get_new_messages.py
UMask=0077
```

Create `~/.config/systemd/user/myguichet-watcher.timer`:

```ini
[Unit]
Description=Check MyGuichet inbox every 15 minutes

[Timer]
OnBootSec=2m
OnUnitActiveSec=15m
Persistent=true

[Install]
WantedBy=timers.target
```

Enable it:

```sh
systemctl --user daemon-reload
systemctl --user enable --now myguichet-watcher.timer
systemctl --user list-timers myguichet-watcher.timer
journalctl --user -u myguichet-watcher.service -f
```

If it must keep running after you log out of a server, an administrator may
need to enable user lingering:

```sh
loginctl enable-linger "$USER"
```

## Security and troubleshooting

Treat `.env`, `cookie.txt`, `.browser-profile/`, `state.json`, `accounts/`, and
`downloads/` as private data. The scripts set restrictive permissions for new
or used files (`0600`) and directories (`0700`), but a dedicated OS user and
encrypted backups are still good practice. Do not commit, email, or place these
files in unencrypted shared storage.

- **`Browser executable not found`** — run the Playwright Chromium install
  command using the same `.venv/bin/python` interpreter as the watcher.
- **Credentials missing in a scheduled run** — put both LuxTrust values in
  `.env`; a scheduler cannot answer terminal prompts.
- **Session expired** — approve the LuxTrust notification; the watcher retries
  once automatically. If the UI has changed, run the login script with
  `MYGUICHET_HEADLESS=false` to inspect it.
- **Attachment too large** — increase `MYGUICHET_MAX_ATTACHMENT_MB` only if
  you expect that size and have sufficient private disk space.
- **Corrupt `state.json`** — restore the affected account's file from backup.
  Removing it intentionally causes the watcher to treat that account's available
  inbox as unprocessed again.

## Offline checks

The included tests do not contact MyGuichet or use credentials:

```sh
.venv/bin/python -m unittest discover -s tests -v
```
