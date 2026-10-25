# Humanode alert

A small Telegram bot that watches the BioAuth status of your Humanode node and sends you the authentication link
when it expires.

Each node owner installs and runs their own copy, on the same server and with the same Linux user as the node,
because the node's RPC is only reachable locally.

What it does:

- checks `bioauth_status` on the local node every minute
- reminds you 60, 30, 5, 3 and 1 minutes before BioAuth expires
- when BioAuth has expired or the node does not answer, alerts you and repeats the alert every 5 minutes
- tells you when the node is back to normal
- sends the time left until the next BioAuth every day at 12:00, if notifications are enabled
- checks at startup that the node RPC responds. If `RPC_URL` is wrong, it tries `127.0.0.1:9944` and
  `127.0.0.1:9933` and tells you which one to put in the config

The authentication link is generated only when you send `/get_bioauth_link`, never automatically. Requesting
links too often makes the node hang. The command runs `humanode-websocket-tunnel` (reused if it is already
running) and `humanode-peer bioauth auth-url` from your Humanode workspace.

It is a single Python file and needs only `python3`, with no extra packages.

## 1. Create a Telegram bot

Open [@BotFather](https://t.me/BotFather), send `/newbot`, choose a name and a username, and copy the token.
Keep the token secret.

## 2. Install

On the server where the node runs:

```bash
git clone https://github.com/whisky098/humanode-alert.git
cd humanode-alert
sudo ./install.sh
```

The installer asks for the bot token, the Linux user that runs the node and the Humanode workspace, then
starts the `humanode-alert` systemd service.

## 3. Register

Open your bot in Telegram and send `/register`. From then on you receive the alerts in that chat. Messages from
any other chat are ignored.

To register from a different chat later, reset the registration:

```bash
sudo rm /var/lib/humanode-alert/state.json && sudo systemctl restart humanode-alert
```

## Commands

- `/register`: receive the alerts in this chat
- `/timezone {time_zone}`: timezone for the displayed times and the daily report, for example
  `/timezone Europe/Chisinau` or `/timezone UTC+3`. The default is UTC.
  [List of time zones](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones)
- `/enable_notification`: enable the daily report at 12:00
- `/disable_notification`: disable the daily report at 12:00
- `/get_bioauth_link`: get a new authentication link
- `/status`: time left until the next BioAuth
- `/help`: list of commands

## Configuration

Settings are stored in `/etc/humanode-alert/config.env`. See [config.env.example](config.env.example). After
editing the file, restart the service:

```bash
sudo systemctl restart humanode-alert
```

The chat id, timezone and notification setting come from the Telegram commands and are kept in
`/var/lib/humanode-alert/state.json`.

## Logs, update, uninstall

```bash
journalctl -u humanode-alert -f              # logs
git pull && sudo ./install.sh                # update, keeps your config and registration
sudo ./install.sh uninstall                  # remove the service
```

To run it without systemd, for example while testing:

```bash
python3 humanode_alert.py config.env
```

## Migrating from the old Java version

The old version was a Java application started with `java -jar` inside `screen`, from
`/srv/humanode/humanode-alert`. The new version replaces it completely.

1. Stop the old application, so that two bots do not run with the same token:

   ```bash
   screen -r humanode-alert     # then press Ctrl+C and type exit
   ```

2. Copy your bot token from the old config. You can reuse the same bot:

   ```bash
   grep bot.token /srv/humanode/humanode-alert/application.properties
   ```

3. Remove the old directory and install the new version, following [Install](#2-install):

   ```bash
   rm -rf /srv/humanode/humanode-alert
   cd /srv/humanode
   git clone https://github.com/whisky098/humanode-alert.git
   cd humanode-alert
   sudo ./install.sh
   ```

4. In Telegram, send `/register` to the bot again, and `/timezone` if you had set one. The old `cache.json` is
   not imported, and the timezone is now UTC by default.

5. Optionally, remove Java if nothing else on the server uses it: `sudo apt-get remove openjdk-17-jdk`.

## License

[MIT](LICENSE)
