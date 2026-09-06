# Document inbox watcher

This small, unofficial tool checks configured document inboxes, fetches
documents it has not processed before, and sends those documents to configured
outputs. It ships with MyGuichet and DKV/Lalux EasyApp source plugins, plus a
folder output plugin.

MyGuichet uses the normal MyGuichet/LuxTrust login flow: the username and
password can be entered by Playwright, but the LuxTrust mobile/device approval
is always required. It does not bypass MFA.

The project is designed for a private macOS or Linux machine with Python 3.10+.

> MyGuichet does not provide this project as an official API client. The portal
> and authentication UI can change, so use it only for your own account and
> keep an eye on its normal terms of use.

## What the files do

| File | Responsibility |
| --- | --- |
| `document_watcher.py` | Main command: resolves configured document accounts and runs the watcher. |
| `watcher_core.py` | Source/output-agnostic polling, state, locking, staging, size-limit, and checkpoint handling. |
| `sources/base.py` | Small protocol a document source implements. |
| `sources/dkv/` | DKV/Lalux EasyApp source plugin: config, OAuth/SMS OTP client, reimbursement adapter. |
| `sources/myguichet/` | MyGuichet source plugin: config, API client, LuxTrust login, source adapter, and probe command. |
| `outputs/base.py` | Small protocol a document output implements. |
| `outputs/folder/` | Folder output plugin: path/mode config and atomic local file delivery. |
| `mqtt_trigger.py` | Long-running MQTT subscriber that triggers one-account or all-account polling. |
| `config.py` | Generic `.env` account/source/output wiring. |
| `storage.py` | Private atomic file writes and a lock that prevents overlapping runs. |

Runtime data is deliberately kept out of Git:

- `.env` - credentials and optional configuration.
- `accounts/` - per-account runtime files such as `cookie.txt`, `state.json`,
  `.browser-profile/`, and `.run.lock`.
- `downloads/` - default folder-output destination.

## Install

Run all commands from this directory:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
cp .env.example .env
chmod 600 .env
```

On a Linux server, Playwright may also need OS browser libraries:

```sh
.venv/bin/python -m playwright install --with-deps chromium
```

## Docker

Build the image:

```sh
docker compose build
```

Run one poll with an interactive terminal, which is required when a source asks
for an OTP:

```sh
docker compose run --rm document-watcher --account alice_dkv
```

Run the configured service once without overriding arguments:

```sh
docker compose up
```

The image contains the application code. Runtime files are mounted separately:

- `document-watcher-accounts:/app/accounts` for cookies, OAuth tokens, browser
  profiles, locks, and state.
- `./downloads:/app/downloads` for the default folder output.

Set `DOCUMENT_RUN_MODE=mqtt` in `.env` to run the MQTT subscriber instead of a
one-shot poll. You can also set it for a single Compose invocation:

```sh
DOCUMENT_RUN_MODE=mqtt docker compose up document-watcher
```

## Configuration Model

Configuration is read through a small provider contract. The default provider is
`EnvConfigProvider`, which keeps the current `.env` syntax and environment
variable behavior. Future JSON or SQLite/UI-backed providers should return the
same `SourceAccountConfig` objects, while source and output plugins keep owning
validation for their own settings.

There is one user list:

```dotenv
DOCUMENT_USERS=alice,bob
```

Each user can have one or more source services. If the source plugin is
registered and there are settings with that source prefix, the source can be
inferred. You can also set `DOCUMENT_<USER>_SOURCES` explicitly.
Users do not need to have the same sources.

For a user/source pair, settings have this shape:

```dotenv
DOCUMENT_<USER>_SOURCES=<source-plugin>,<source-plugin>
DOCUMENT_<USER>_<SOURCE_PLUGIN>_<SOURCE_SETTING>=...
DOCUMENT_<USER>_<SOURCE_PLUGIN>_OUTPUTS=<output-type> or <output-name>:<output-type>
DOCUMENT_<USER>_<SOURCE_PLUGIN>_OUTPUT_<OUTPUT_NAME>_<OUTPUT_SETTING>=...
```

User/source-level settings:

```dotenv
DOCUMENT_<USER>_<SOURCE_PLUGIN>_RUNTIME_DIR=accounts/<user>_<source>
DOCUMENT_<USER>_<SOURCE_PLUGIN>_MAX_DOCUMENT_MB=100
```

`RUNTIME_DIR` defaults to `accounts/<account>`. `MAX_DOCUMENT_MB` defaults to
`100` and applies before a document is handed to outputs. Source and output
plugins validate their own settings after generic wiring has selected them.

## Full Example

This example has:

- users: Alice and Bob
- source plugins: `myguichet`, `dkv`, and a made-up `otherservice`
- output plugins: the built-in `folder` output and a made-up `example_api`
- Alice uses only `myguichet`
- Bob uses both `myguichet` and `otherservice`

```dotenv
DOCUMENT_USERS=alice,bob
DOCUMENT_ALICE_SOURCES=myguichet
DOCUMENT_BOB_SOURCES=myguichet,dkv,otherservice

# Alice: MyGuichet -> folder + API
DOCUMENT_ALICE_MYGUICHET_LUXTRUST_USERNAME=alice-luxtrust-user
DOCUMENT_ALICE_MYGUICHET_LUXTRUST_PASSWORD=alice-luxtrust-password
DOCUMENT_ALICE_MYGUICHET_SPACE_ID=123456
DOCUMENT_ALICE_MYGUICHET_LANGUAGE=fr
DOCUMENT_ALICE_MYGUICHET_HEADLESS=true
DOCUMENT_ALICE_MYGUICHET_OUTPUTS=local:folder
DOCUMENT_ALICE_MYGUICHET_OUTPUT_LOCAL_DIRECTORY=/srv/documents/alice/myguichet
DOCUMENT_ALICE_MYGUICHET_OUTPUT_LOCAL_FILE_MODE=0644

# Bob: MyGuichet -> folder only
DOCUMENT_BOB_MYGUICHET_LUXTRUST_USERNAME=bob-luxtrust-user
DOCUMENT_BOB_MYGUICHET_LUXTRUST_PASSWORD=bob-luxtrust-password
DOCUMENT_BOB_MYGUICHET_SPACE_ID=789012
DOCUMENT_BOB_MYGUICHET_LANGUAGE=fr
DOCUMENT_BOB_MYGUICHET_HEADLESS=true
DOCUMENT_BOB_MYGUICHET_OUTPUTS=local:folder
DOCUMENT_BOB_MYGUICHET_OUTPUT_LOCAL_DIRECTORY=/srv/documents/bob/myguichet

# Bob: DKV/Lalux EasyApp treated reimbursements -> folder only
DOCUMENT_BOB_DKV_USERNAME=bob-dkv-user
DOCUMENT_BOB_DKV_PASSWORD=bob-dkv-password
DOCUMENT_BOB_DKV_OTP_TIMEOUT_SECONDS=300
DOCUMENT_BOB_DKV_OUTPUTS=local:folder
DOCUMENT_BOB_DKV_OUTPUT_LOCAL_DIRECTORY=/srv/documents/bob/dkv

# Bob: OtherService -> folder + API
DOCUMENT_BOB_OTHERSERVICE_USERNAME=bob-other-user
DOCUMENT_BOB_OTHERSERVICE_PASSWORD=bob-other-password
DOCUMENT_BOB_OTHERSERVICE_OUTPUTS=local:folder,archive:example_api
DOCUMENT_BOB_OTHERSERVICE_OUTPUT_LOCAL_DIRECTORY=/srv/documents/bob/other
DOCUMENT_BOB_OTHERSERVICE_OUTPUT_ARCHIVE_URL=https://api.example.invalid/documents
DOCUMENT_BOB_OTHERSERVICE_OUTPUT_ARCHIVE_TOKEN=bob-api-token
```

The `example_api` and `otherservice` plugins above are illustrative names; this
repo currently implements `myguichet` and `dkv` sources and the `folder` output.

## Folder Output

The folder output saves staged documents atomically into a local directory.

```dotenv
DOCUMENT_<USER>_<SOURCE_PLUGIN>_OUTPUTS=local:folder
DOCUMENT_<USER>_<SOURCE_PLUGIN>_OUTPUT_LOCAL_DIRECTORY=downloads/<user>_<source>
DOCUMENT_<USER>_<SOURCE_PLUGIN>_OUTPUT_LOCAL_FILE_MODE=0600
DOCUMENT_<USER>_<SOURCE_PLUGIN>_OUTPUT_LOCAL_DIR_MODE=0700
```

If an account omits `OUTPUTS`, it defaults to one output named `folder` of type
`folder`, writing to `downloads/<account>`.

Existing output directories are not chmodded. This matters for Docker bind
mounts and NFS shares such as a Paperless-ngx consume folder: set the folder
permissions/ownership on the host, then choose a downloaded file mode that the
consumer can read.

## MyGuichet Source

MyGuichet source settings live under:

```dotenv
DOCUMENT_<USER>_MYGUICHET_<SETTING>=...
```

Supported settings:

```dotenv
DOCUMENT_<USER>_MYGUICHET_LUXTRUST_USERNAME=your-user-id
DOCUMENT_<USER>_MYGUICHET_LUXTRUST_PASSWORD=your-password
DOCUMENT_<USER>_MYGUICHET_SPACE_ID=your-space-id
DOCUMENT_<USER>_MYGUICHET_LANGUAGE=fr
DOCUMENT_<USER>_MYGUICHET_HEADLESS=true
DOCUMENT_<USER>_MYGUICHET_LOGIN_TIMEOUT_SECONDS=300
```

`SPACE_ID` is required and different for every MyGuichet account. To find one,
log in once with the account's `HEADLESS=false`, open DevTools > Network, and
look at any `/fpgun-iep-api/api/space/v1/...` request URL; the number in the
path is your space ID.

If the username or password is omitted, an interactive run prompts for the
missing value without saving it. A scheduler has no interactive terminal, so an
unattended setup must put both values in `.env`.

## DKV/Lalux EasyApp Source

The `dkv` source uses the Lalux EasyApp API seen by the web frontend. It
retrieves only reimbursements whose status code is `TREATED`; submitted/sent
reimbursements are intentionally skipped.

```dotenv
DOCUMENT_<USER>_DKV_USERNAME=your-login
DOCUMENT_<USER>_DKV_PASSWORD=your-password
DOCUMENT_<USER>_DKV_OTP_TYPE=SMS
DOCUMENT_<USER>_DKV_OTP_TIMEOUT_SECONDS=300
DOCUMENT_<USER>_DKV_PAGE_LIMIT=20
```

The first login submits username/password, requests an SMS OTP, and waits for
that OTP through the generic external-input broker. Successful OAuth tokens are
stored in the account runtime directory as `dkv_token.json`; later runs reuse
the refresh token until the service rejects or expires it.

## External Input and OTP

Source plugins can request human-provided data through the source context:

```python
answer = context.request_input(
    InputChallenge(
        account_name=account.name,
        source=account.source,
        kind="otp",
        prompt="Enter the 6-digit one-time code",
        timeout_seconds=300,
    )
)
code = answer["code"]
```

The timeout belongs in the challenge and should match the service token
validity window. If the answer does not arrive in time, the current account
fails with a clear source error instead of hanging the whole watcher.

The one-shot command uses a CLI input broker: it can prompt on an interactive
terminal and times out otherwise. The MQTT runner uses a push input broker, so
another device can publish the requested fields without first listing open
sessions.

## Run Once

Poll every configured account:

```sh
.venv/bin/python document_watcher.py
```

Poll one account:

```sh
.venv/bin/python document_watcher.py --account alice_myguichet
```

The watcher:

1. Asks the source plugin for unseen messages.
2. Stages each document into a private temporary file under the account runtime
   directory.
3. Delivers the staged file to all configured output plugins.
4. Records the message in that account's `state.json` only after all outputs
   succeed.

If one message fails, later messages in the same run are still attempted.
Successful messages are checkpointed. The delivery model is **at least once**:
if a process is interrupted after delivery but before checkpointing, that
message can be delivered again on the next run.

For MyGuichet login troubleshooting only:

```sh
.venv/bin/python -m sources.myguichet.login --account alice_myguichet
```

Do not run that command while the regular watcher is running for the same
account, because both use that account's private Chromium profile.

## MQTT Trigger

`mqtt_trigger.py` is a long-running subscriber. It connects to an existing
broker and triggers the same polling code used by the one-shot command.

```dotenv
DOCUMENT_MQTT_HOST=localhost
DOCUMENT_MQTT_PORT=1883
DOCUMENT_MQTT_TOPIC=documents/poll
DOCUMENT_MQTT_WORKERS=1
DOCUMENT_MQTT_INPUT_TOPIC=documents/input/provide
DOCUMENT_MQTT_INPUT_TTL_SECONDS=300
DOCUMENT_MQTT_STATUS_TOPIC=documents/poll/status
```

Run it with:

```sh
.venv/bin/python mqtt_trigger.py
```

Accepted trigger payloads:

- empty payload, `all`, or `*` - poll all configured accounts.
- `alice_myguichet` - poll one configured account.
- `{"account":"alice_myguichet"}` - poll one account.
- `{"accounts":["alice_myguichet","bob_other"]}` - poll selected accounts.

Sources that need external input, such as an OTP, use the same generic input
broker as the CLI. With MQTT, publish the answer directly to
`DOCUMENT_MQTT_INPUT_TOPIC`; the payload is keyed by configured account name:

```sh
mosquitto_pub -h localhost -t documents/input/provide -m '{"for":"alice_dkv","code":"123456"}'
```

`code` is only shorthand for the common one-field OTP case. It is equivalent to
`"fields":{"code":"123456"}` and works when the source asks for a field named
`code`.

For non-OTP prompts, or sources that request multiple values, use `fields`:

```sh
mosquitto_pub -h localhost -t documents/input/provide -m '{"for":"bob_otherservice","fields":{"answer":"blue","device":"phone"}}'
```

The MQTT broker accepts answers even before the source asks for them and keeps
them for `DOCUMENT_MQTT_INPUT_TTL_SECONDS` seconds. Each source challenge also
has its own timeout; if no valid answer arrives before then, that account fails
cleanly and the MQTT process keeps running. Set `DOCUMENT_MQTT_WORKERS` above
`1` if you want multiple account polls, and therefore multiple input prompts,
to run at the same time. Different accounts can wait for input at the same
time. A second simultaneous challenge for the same account is rejected because
the blind-push key would otherwise be ambiguous.

If `DOCUMENT_MQTT_STATUS_TOPIC` is set, the orchestration layer publishes JSON
events there. Source and output plugins do not need to implement status
publishing themselves. A successful account poll ends with:

```json
{
  "schema": "document_watcher.status.v1",
  "event": "poll.finished",
  "status": "ok",
  "timestamp": "2026-09-05T12:00:00Z",
  "account": "alice_dkv",
  "source": "dkv",
  "new_messages": 3
}
```

Errors use the same shape with `"status":"error"` plus `error_type` and
`error_message`. The runner also emits `poll.queued`, `poll.started`,
`input.accepted`, and rejected-trigger/input events.

In Docker, set `DOCUMENT_RUN_MODE=mqtt` in `.env` or before `docker compose up`
to run the MQTT subscriber instead of a one-shot poll.

## Run Regularly

The main command is safe to schedule. It uses a local lock, so a second run
exits cleanly when the previous one is still working. It also performs one
automatic authentication refresh/retry when a source reports an expired session.

For a simple check every 15 minutes, edit your user crontab with `crontab -e`:

```cron
*/15 * * * * umask 077; cd /path/to/document-inbox-watcher && /path/to/document-inbox-watcher/.venv/bin/python document_watcher.py >> /path/to/document-inbox-watcher/watcher.log 2>&1
```

## Add Plugins

A source adapter implements the protocol in `sources/base.py`: list unseen
messages, list documents for one message, open a streaming document response,
refresh authentication, and close resources. Source-specific settings use:

```dotenv
DOCUMENT_<USER>_<SOURCE_PLUGIN>_<SETTING>=...
```

An output adapter implements the protocol in `outputs/base.py`: deliver one
staged local document and close resources. Output-specific settings use:

```dotenv
DOCUMENT_<USER>_<SOURCE_PLUGIN>_OUTPUT_<OUTPUT_NAME>_<SETTING>=...
```

Outputs receive documents one at a time through `deliver(...)`. An output that
needs a poll-level batch, such as "zip everything from this run", can also
implement optional lifecycle hooks:

```python
def begin_poll(account, config) -> None: ...
def end_poll(account, config, result) -> None: ...
```

`result` is an `OutputPollResult` with `processed_messages`,
`delivered_documents`, and `failed_messages`. When any configured output has an
`end_poll` hook, the watcher defers checkpointing successful messages until
after all `end_poll` hooks succeed. This avoids marking documents as processed
before a batch output has finalized its archive or upload.

Every network or browser operation inside a plugin should have its own clear
timeout. Plugin errors should raise `SourceError` or `OutputError` with a short,
actionable message.

## Security and Troubleshooting

Treat `.env`, `cookie.txt`, `.browser-profile/`, `state.json`, `accounts/`, and
`downloads/` as private data unless you intentionally point an output at a
shared consume folder. Runtime files remain private.

- **`Browser executable not found`** - run the Playwright Chromium install
  command using the same `.venv/bin/python` interpreter as the watcher.
- **Credentials missing in a scheduled run** - put source credentials in `.env`;
  a scheduler cannot answer terminal prompts.
- **Session expired** - approve the LuxTrust notification; the watcher retries
  once automatically.
- **Document too large** - increase `DOCUMENT_<USER>_<SOURCE>_MAX_DOCUMENT_MB` only if
  you expect that size and have sufficient private disk space.
- **Corrupt `state.json`** - restore the affected account's file from backup.
  Removing it intentionally causes the watcher to treat that account's available
  inbox as unprocessed again.

## Offline Checks

The included tests do not contact external services or use credentials:

```sh
.venv/bin/python -m unittest discover -s tests -v
```
