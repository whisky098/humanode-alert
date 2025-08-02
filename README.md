# Humanode alert

## IMPORTANT

The application must be started on the same machine as the humanode, and started with the same user.

If you have this warn in console: `Chat id not found, need to register, to register type /register in chat with bot`,
you are not registered.

## Documentation

- [Create new bot](#create-new-bot)
- [Configuration](#configuration)
- [Run application in screen](#run-application-in-screen)
- [Update application](#update-application)
- [Register to receive alerts](#register-to-receive-alerts)
- [Help](#help)
- [List of all time zones](#list-of-all-time-zones)

### Create new bot

--------------------------------------------------------------------------------
Go to telegram and find @BotFather (https://t.me/BotFather)

From menu select `/newbot`

![img_9.png](img_9.png)

Type name and bot username, and you will get a new bot with token

`In the screen I have hidden the token (don't publish it anywhere, it's a secret token linked to your account)`

![img_10.png](img_10.png)

--------------------------------------------------------------------------------

### Configuration

--------------------------------------------------------------------------------
Install openjdk-17-jdk: `sudo apt-get install openjdk-17-jdk`

--------------------------------------------------------------------------------
Create directory: `mkdir /srv/humanode/`

Go to directory: `cd /srv/humanode/`

Clone jar from git: `git clone --branch build https://github.com/whisky098/humanode-alert.git`

Go to directory: `cd humanode-alert`

Edit application.properties and put your Telegram bot token: `bot.token=your_bot_token`

--------------------------------------------------------------------------------

### Run application in screen

Create new screen: `screen -S humanode-alert`

Run application: `java -Dspring.config.location=/srv/humanode/humanode-alert/application.properties -jar humanode-alert-1.0.0.jar`

Exit from screen: `CTRL+A+D`

--------------------------------------------------------------------------------

### Update application

Resume screen: `screen -r humanode-alert`

Stop application: `CTRL+C`

Stash changes: `git stash`

Update targets: `git pull`

Check application properties if bot token is present

Run application: `java -Dspring.config.location=/srv/humanode/humanode-alert/application.properties -jar humanode-alert-1.0.0.jar`

Exit from screen: `CTRL+A+D`

Send again register command in telegram chat with bot `/register`

To view all commands type `/help`

--------------------------------------------------------------------------------

### Register to receive alerts

To receive alerts from this bot you must submit the command `/register`.     Access the bot from the message you received
from BotFather.

![img_7.png](img_7.png)

You will receive a confirmation message

![img_6.png](img_6.png)
--------------------------------------------------------------------------------

### HELP

Resume screen: `screen -r humanode-alert`

List of commands:

- `/register`
- `/timezone {time_zone_id}`
- `/enable_notification`
- `/disable_notification`
- `/get_bioauth_link`
- `/help`

### List of all time zones

[List of all time zones](https://garygregory.wordpress.com/2013/06/18/what-are-the-java-timezone-ids/)
