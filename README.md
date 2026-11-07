# Queue Sniper (mctiers / pvptiers style)

Watches chosen Discord channels and **auto-clicks the "Enter Waitlist" / "Join Queue"
button** the instant the queue message appears. It then plays a loud alarm and
(optionally) says *"Queue open. Go!"* out loud, so you are not sitting in silence.

```
[21:14:03.118] HIT     DETECTED  mctiers / #queue  button='Enter Waitlist'  age=41ms
[21:14:03.166] OK      CLICKED   ok  http=48ms  total=89ms
```

---

## Read this first

- This logs in with **your Discord user token** (a "self-bot"). That is **against
  Discord's Terms of Service** and can get the account **banned**.
- Use an **alt account** if you care about your main.
- "Queue sniping" can also be against the rules of the specific server. You can be
  banned from the server even if Discord does not ban you.
- Your token is full access to your account. Never share it, never commit it.
  It is stored locally in `.env`, which is git-ignored.
- This tool does not bypass anything. It only clicks a button your account could
  click manually. If the queue is closed, role-locked, or you are already in it,
  the click will fail and you will hear the error beep.
- If the matched button opens a **modal form** (a pop-up), the tool can press the
  button but **cannot fill in or submit the form**. Some "Enter Waitlist" panels
  work this way. Point the sniper at the message that has the real queue-open
  button (usually labelled **"Join Queue"**). See
  [Modal buttons](#modal-buttons).

---

## Quick start

### The easy way (no Python needed)

Double-click **`QueueSniper.exe`**.

- On the first launch it asks for your **token**, then lets you add the **server ID
  and channel ID** of each waitlist channel, and whether to auto-click or only alarm.
- After that, double-clicking simply starts the sniper. It waits **4 seconds** on
  startup: press **S** during that time to change the watch list, or do nothing
  to start.
- You can also double-click **`setup.bat`** any time to open the same menu.
- `config.json`, `.env`, `logs/` and `alarm.wav` are created/read **next to the
  exe**, so you can copy `QueueSniper.exe` into its own folder.

The exe accepts the same flags as the scripts:

```bat
QueueSniper.exe --setup
QueueSniper.exe --list
QueueSniper.exe --dry-run
QueueSniper.exe --debug
QueueSniper.exe --test-alarm
QueueSniper.exe --self-test
```

If Windows SmartScreen warns about an unknown publisher (normal for unsigned
programs), click **More info → Run anyway**.

### From source (Python)

1. Install Python 3.12 (already done on this PC) and dependencies:

   ```bat
   install.bat
   ```

2. Add the servers and channels to watch:

   ```bat
   run.bat --setup
   ```

3. Start it:

   ```bat
   run.bat
   ```

That's it. Leave the window open. Press `Ctrl+C` to stop.

---

## Getting your Discord token

Use a browser, not the desktop app:

1. Open <https://discord.com/app> and log in.
2. Press `F12` → **Application** tab → **Local Storage** → `https://discord.com`.
3. Find the key named `token` and copy its value **without the surrounding quotes**.
4. Alternative: **Network** tab → click any request to `/api/...` → **Headers** →
   copy the `authorization` value.

The token changes if you change your password or log out all devices. If login
stops working, grab a fresh token.

`QueueSniper.exe --setup` (or the first launch) saves it to `.env`. You can also
create `.env` yourself:

```
DISCORD_TOKEN=your_token_here
```

---

## Finding the server and channel IDs

1. In Discord: **Settings → Advanced → Developer Mode** → turn it **ON**.
2. Right-click the **server** → **Copy Server ID**.
3. Right-click the **waitlist channel** → **Copy Channel ID**.

Paste them when the setup wizard asks, or put them in `config.json`:

```json
{
  "server_id": "1234567890123456789",
  "channel_id": "9876543210987654321"
}
```

Channel IDs are globally unique; the server ID is used to check the channel is in
the right place. To watch more than one channel, add them to `watch_channels`.
If those channels are spread across several servers, also list the servers in
`watch_servers`:

```json
{
  "server_id": "",
  "channel_id": "",
  "watch_servers": {
    "111111111111111111": "Server One",
    "222222222222222222": "Server Two"
  },
  "watch_channels": {
    "333333333333333333": "",
    "444444444444444444": ""
  }
}
```

Leave `server_id` and `channel_id` empty when using the multi-server layout;
`watch_channels` is the list that is actually watched. Empty channel names are
fine — the sniper logs the real channel name once it connects.

Not sure which ID is which? Run `QueueSniper.exe --list` (or `run.bat --list`)
after adding your token; it prints every server and channel with its ID.

`config.json` and `.env` are **local-only** and listed in `.gitignore`: they hold
your server/channel IDs and your token. To start from the template instead:

```bat
copy config.example.json config.json
copy .env.example .env
```

### Managing the list in the terminal

Run `QueueSniper.exe --setup` (or double-click `setup.bat`) whenever you want to
change what is watched. You can also double-click `QueueSniper.exe` and press
**S** during the 4-second startup countdown. It prints the current list and a
simple menu:

```
  [1] Add a server + channel
  [2] Remove a channel
  [3] Clear the whole list
  [4] Done - save and start
```

When adding, you can either:

- paste a **channel link** (`https://discord.com/channels/…/…`), or
- type the **Server ID**, then the **Channel ID**.

Channel IDs are enough on their own; the server ID is used to warn you if a
channel is not where it was expected. Nothing is deleted unless you choose it,
and the sniper starts automatically after you pick **Done**.

You can still edit `config.json` by hand if you prefer; the menu uses the same
`watch_servers` / `watch_channels` keys.

---

## Commands

`QueueSniper.exe` accepts the same flags as `run.bat`. Replace `run.bat` with
`QueueSniper.exe` if you are using the built exe.

| Command | What it does |
|---|---|
| `run.bat` | Run the sniper |
| `run.bat --setup` | Add/remove the servers and channels to watch (terminal menu) |
| `run.bat --list` | Print every server/channel with its ID |
| `run.bat --dry-run` | Alarm only, never click (safe testing) |
| `run.bat --debug` | Print every button/message seen in watched channels |
| `run.bat --test-alarm` | Play the alarm + voice |
| `run.bat --self-test` | Check the matching logic without logging in |

Run these from a normal terminal if you prefer:

```bat
.venv\Scripts\python.exe sniper.py --debug
```

---

## Tuning what gets clicked

Open `config.json`.

Buttons are matched against **`"<label> <custom_id>"`**. A button is clicked when:

1. it is enabled, not a link, and has a `custom_id`,
2. it matches at least one `button_patterns` regex,
3. it matches **none** of the `deny_patterns` regex.

The defaults already cover:

- `Enter Waitlist`
- `Join Waitlist`
- `Enter Queue`
- `Join Queue`
- `Click to Join`
- `Join Now`

and explicitly ignore `Leave`, `Exit`, `Quit`, `Cancel`, `View`, `Check`.

If the server uses a different label, run with `--debug` and look at the output:

```
[debug] mctiers / #queue: 'Enter Waitlist'/'join_waitlist', 'Leave Waitlist'/'leave_waitlist'
```

Add the label or `custom_id` to `button_patterns` (regex, case-insensitive).

Other useful settings:

| Key | Meaning |
|---|---|
| `server_id` | Server that contains the waitlist channel (optional, used to check it) |
| `channel_id` | The waitlist channel to watch |
| `watch_servers` | Multiple servers, as `"server_id": "name"`. Use this when the watched channels are in different servers |
| `watch_channels` | Channels to watch, as `"channel_id": "name"`. This is the list used for multi-channel watching |
| `watch_threads` | Also watch threads under a watched channel |
| `click_cooldown_seconds` | Minimum gap between clicks in one channel |
| `rearm_after_seconds` | Re-click the same button after N seconds (`0` = only after it is seen disabled and re-enabled) |
| `retry_attempts` | Retries if Discord returns a transient error |
| `keepalive_seconds` | Sends a tiny request every N seconds so the API connection (HTTP/3) stays warm and the click skips the TCP/TLS handshake. `0` = off |
| `boost_process` | Raise process priority and use a 1 ms Windows timer while running |
| `dry_run` | `true` = never click |
| `alarm.repeat` | How many times the beep plays |
| `alarm.voice` | Spoken "Queue open" alert |
| `alarm.flash_taskbar` | Flash the console in the taskbar |
| `startup_sound` | Beep when the sniper connects |
| `log_to_file` | Also write logs to `logs/` |

---

## Troubleshooting

**No reaction at all**
- Run `run.bat --list` and confirm the channel ID exists (it prints every server
  and channel with its ID). Compare it with `server_id` / `channel_id` in
  `config.json`.
- Run `run.bat --debug`. If nothing prints, the account is not receiving messages
  from that channel. Make sure the account is actually in the server and can read
  the channel.
- In very large servers, Discord sometimes does not push messages for channels you
  have never opened. Open the queue channel once in the Discord client (or the
  browser) with the same account.

**It sees the message but does not click**
- The button label/`custom_id` does not match. Copy it from `--debug` output into
  `button_patterns`.
- The button is disabled. The tool waits for the edit that enables it and clicks
  then. If the server never enables it, nothing happens.
- If the server reuses the **same message** and only toggles the button, the
  sniper re-arms itself when it sees the button disabled again, so it can click
  the next time the queue opens. If it still does not, set
  `rearm_after_seconds` (e.g. `120`) to allow a re-click on a timer.

### Modal buttons

Some panels (notably the mctiers "Enter Waitlist" message) open a **modal form**
when pressed — a pop-up asking for your region/kit/username. A self-bot can press
the button, but it would have to fill in and submit that form, which this tool
does not do. You will see `CLICKED ok` but nothing else happens.

Fix: watch for the message that has the actual **"Join Queue"** button (the one
that opens when the queue is live) and select that channel with `--setup`. Run
`--debug` when a queue opens and compare the buttons on each message.

**"CLICK FAILED" / "Interaction failed"**
- The queue closed before the click landed, you are already in the queue, or your
  account lacks the required role. The tool cannot fix that.
- If it happens every time, run `--dry-run` and compare with a manual click.

**No sound**
- Run `run.bat --test-alarm`.
- Check the Windows volume mixer and the default output device.
- The generated alarm is `alarm.wav`; you can replace it with any WAV file.

**Token rejected**
- Re-copy the token. Changing your password invalidates it.
- Make sure you copied the token, not the "Refresh Token" or the bot token.
- Do not put quotes around it in `.env`.

**Windows blocks or deletes the exe**
- Unsigned PyInstaller exes are sometimes flagged by SmartScreen/Defender. For
  SmartScreen click **More info → Run anyway**. If Defender removes it, add an
  exclusion for the folder, or just run from source with `run.bat`.

**Rate limited / weird behaviour**
- Do not run more than one copy of the sniper on the same account.
- Increase `click_cooldown_seconds` if you are in many queue channels.

---

## How it works (short version)

It connects to Discord's gateway as your account, listens for `MESSAGE_CREATE` and
`MESSAGE_UPDATE` in the watched channels, walks the message components (including
Components V2 containers), matches the button, and posts the same component
interaction your client would post when you click it.

The click path is kept short:

- **Warm connection.** Discord's API is reached over HTTP/3. A cold request pays a
  QUIC/TLS handshake first (~150-160 ms); a warm one skips it. The sniper sends a
  tiny authenticated `GET /users/@me` every `keepalive_seconds` (default 20 s) so
  the **origin** connection and session the click uses are already open. (The
  `/gateway` endpoint is edge-served, so it does not warm the click path.)
- **Click before logging.** The interaction POST is started before the `DETECTED`
  line and before the alarm thread is spawned.
- **Non-blocking log file.** Log lines are queued to a writer thread, so disk I/O
  never sits in front of the POST.
- **Above-normal process priority** (and a 1 ms timer) so a busy game on the same
  PC does not delay the handler.

After that, latency is dominated by the Discord **origin** round trip. Some parts
of Discord are edge-served (e.g. `GET /gateway` answers in ~15 ms), but
authenticated routes such as `POST /interactions` go to the origin server. From
New Zealand that is roughly **300 ms just for the network round trip**, plus
Discord's own processing time, so a total of ~450-700 ms is normal here. It is not
instant, and other people using similar tools may still beat you.

### Reading the timing in the log

```
[13:52:21.707] HIT  DETECTED  #queue  button='Join Queue'  age=1016ms  recv=3ms
[13:52:22.170] OK   CLICKED   ok  http=463ms  react=466ms  total=1479ms
```

| Field | Meaning |
|---|---|
| `age` | How old the message was when it was detected, using Discord's clock (corrected for PC clock drift). This is the time the queue had already been open. |
| `recv` | Time from the gateway event reaching the program to the detection being logged. Should be a few ms. |
| `http` | Time for Discord to answer the click (`POST /interactions`). This is the network round trip + Discord processing. |
| `react` | Time from receiving the event to the click being confirmed. |
| `total` | `age + react` — the true end-to-end delay from the message being posted to the click succeeding. |

If `age` looks huge or `total` seems ~0.8 s worse than reality, your PC clock is
off. The sniper measures the offset with NTP at startup, applies it to the log
values, and prints a warning when the drift is over 0.25 s. Syncing Windows time
(Settings → Time & language → Date & time → **Sync now**) keeps the numbers exact.

A `total` of **~680 ms** is about the floor from New Zealand: ~200 ms for the
message to arrive over the gateway, ~450 ms for the interaction round trip.
Running the sniper on a VPS in the US or Sydney would cut this further, but it
also means moving the token to that machine.

---

## Building the .exe

The exe is already built at `QueueSniper.exe`. To rebuild after editing
`sniper.py`:

```bat
build.bat
```

It installs PyInstaller into `.venv` and writes `QueueSniper.exe` into the
project folder. Build junk goes to `build/` and can be deleted.

---

## Files

```
QueueSniper.exe    standalone build - double-click to run
setup.bat          double-click to add/remove watched channels
sniper.py          main program (source)
config.json        your channels/patterns/alarm settings (local, git-ignored)
config.example.json  blank config template (safe to commit)
.env               your token (local, git-ignored)
.env.example       blank token template (safe to commit)
requirements.txt   dependencies
install.bat        one-time Python setup
build.bat          rebuilds QueueSniper.exe
run.bat            start from source / commands
build/             PyInstaller temp files (safe to delete)
logs/              daily logs
alarm.wav          generated on first run
```

---

## Disclaimer

This is a personal automation tool. Automating a user account violates Discord's
Terms of Service and can result in a ban. Using it may also violate the rules of
the server you are sniping. You accept those risks. The author is not responsible
for bans, lost queue spots, or anything else.
