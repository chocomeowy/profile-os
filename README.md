# Weekly OS

A personal operating system check-in that runs every Sunday.

Reads your `profile.md` from Google Drive, sends it to Gemini for reasoning,
then delivers a structured weekly brief to your Telegram and email.

What makes it useful:
- Reads your free-text Telegram messages from the past week as context
- Warns you if your profile hasn't been updated in 14+ days
- Maintains a goals log on Drive with automatic compression at 30 days and 1 year
- Designed to be forked: swap the profile, add your secrets, done

---

## What it does

Every Sunday at 8am SGT, it:
1. Reads your Telegram messages from the past 7 days (free-text notes you sent to your bot)
2. Fetches your `profile.md` from Google Drive
3. Warns if your profile is stale (> 14 days since last update)
4. Reads your `goals_log.md` from Drive for historical context
5. Compresses old log entries (monthly at 30 days, yearly at 1 year) via Gemini
6. Sends everything to Gemini and generates a structured brief
7. Delivers to Telegram and email
8. Clears processed Telegram messages and appends this week's goal status to the log

---

## Setup (one-time)

### Step 1: Fork or clone this repo

Make it private. Your profile contains personal rules and preferences.

### Step 2: Create your profile.md in Google Drive

Write your profile following the template in this repo.
Upload it to Google Drive.
Copy the file ID from the URL: `https://drive.google.com/file/d/FILE_ID_HERE/view`

Edit it on your phone anytime via the Google Drive app.

### Step 3: Create a Google Drive service account

1. Go to https://console.cloud.google.com
2. Create a new project (or use an existing one)
3. Enable the Google Drive API
4. IAM and Admin > Service Accounts > Create Service Account
5. Name it anything (e.g. `weekly-os`)
6. No roles needed
7. Create a JSON key and download it
8. Open your `profile.md` in Drive, click Share, share with the service account email (looks like `name@project.iam.gserviceaccount.com`) with Viewer access

### Step 4: Get a Gemini API key

Go to https://aistudio.google.com/app/apikey and create a key.
The free tier is more than enough for one weekly call.

### Step 5: Set up your Telegram bot

If you don't have one:
- Open Telegram, search @BotFather, send `/newbot`, follow the prompts, copy the token
- Message your bot, then open `https://api.telegram.org/bot<TOKEN>/getUpdates`
- Look for `"chat":{"id":XXXXXXXXX}` — that's your chat ID

### Step 6: Set up Gmail app password

1. Enable 2FA on your Google account
2. Go to https://myaccount.google.com/apppasswords
3. Create an app password for "Mail"
4. Use this as `EMAIL_PASSWORD` (not your actual Gmail password)

### Step 7: Run setup.py once

This creates `goals_log.md` on Drive, seeds it with your current goals,
and clears any Telegram backlog. It prints the file ID you need for Step 8.

```bash
# Set your environment variables first
export GDRIVE_FILE_ID="your-profile-file-id"
export GDRIVE_SERVICE_ACCOUNT_JSON="$(cat your-service-account-key.json)"
export GEMINI_API_KEY="your-gemini-key"
export TELEGRAM_BOT_TOKEN="your-bot-token"
export TELEGRAM_CHAT_ID="your-chat-id"
export OWNER_EMAIL="your@gmail.com"   # optional: share goals_log.md with yourself on Drive

python setup.py
```

Copy the `GDRIVE_GOALS_LOG_FILE_ID` it prints.

### Step 8: Add GitHub Secrets

In your repo: Settings > Secrets and variables > Actions > New repository secret

| Secret name | Value |
|---|---|
| `GDRIVE_FILE_ID` | File ID of your `profile.md` on Google Drive |
| `GDRIVE_SERVICE_ACCOUNT_JSON` | Full contents of your service account JSON key |
| `GDRIVE_GOALS_LOG_FILE_ID` | File ID printed by `setup.py` |
| `GEMINI_API_KEY` | Your AI Studio API key |
| `TELEGRAM_BOT_TOKEN` | Your Telegram bot token |
| `TELEGRAM_CHAT_ID` | Your Telegram chat ID |
| `EMAIL_ADDRESS` | Your Gmail address |
| `EMAIL_PASSWORD` | Your Gmail app password |
| `EMAIL_RECIPIENT` | Where to send the email (can be same as `EMAIL_ADDRESS`) |

### Step 9: Move the workflow file

Make sure `.github/workflows/weekly.yml` exists in your repo.
It is already in the correct location if you cloned this repo.

### Step 10: Test it manually

Go to Actions > Weekly OS Check-in > Run workflow.
Check your Telegram and email. Check `goals_log.md` on Drive for the first appended entry.

---

## How to use it during the week

Send any free-text messages to your Telegram bot throughout the week.
Examples:
- `3 workouts done, diet clean, skipped fish twice`
- `Applied to 6 jobs this week, got one callback from GXS`
- `Missed sleep deadline 3 nights, need to fix this`

The next Sunday brief will read these notes and reference them directly in the Health,
Goals, and Focus sections. After the brief is sent, those messages are cleared.

---

## Keeping your profile current

Open the Google Drive app on your phone, find `profile.md`, and edit directly.
Update the `Last updated` date at the top when you make changes.
If you forget for 14+ days, the brief will call it out.

The profile has five sections:
- **Finance Rules** - thresholds, positions, cash rules
- **Health Habits** - daily and weekly targets, watch flags
- **Personal Preferences** - life context, work targets, lifestyle rules
- **Active Goals** - by date and ongoing
- **Notes for the AI** - instructions to shape how the brief is written

---

## Goals log auto-compression

`goals_log.md` on Drive stores your goal check-ins over time.

- **Past 30 days**: raw weekly entries
- **30 days to 1 year**: compressed into monthly summaries (by Gemini, automatically)
- **Over 1 year**: compressed into yearly summaries (by Gemini, automatically)

The file stays compact indefinitely. You never need to touch it manually.

---

## Customising the brief

Edit `SYSTEM_PROMPT` in `weekly_checkin.py` to change the tone, sections, or output format.
The profile and prompt logic are separate — tune one without touching the other.

---

## Making it for someone else

1. They fork the repo
2. They write their own `profile.md` on their Drive
3. They run `setup.py` with their own secrets
4. They add their own GitHub Secrets
5. Done — no code changes needed unless they want to customise the brief

---

## File structure

```
profile-os/
  setup.py                        run once to initialise
  weekly_checkin.py               main weekly script
  requirements.txt                python dependencies
  .github/
    workflows/
      weekly.yml                  cron schedule and job definition
  README.md                       this file
```

Files that live on Google Drive (not in the repo):
```
  profile.md                      your personal profile (edit freely)
  goals_log.md                    goal history log (managed automatically)
```

---

## Cost

- GitHub Actions: free for public repos, 2000 min/month free for private (this uses ~2 min/week)
- Gemini API: free tier via AI Studio (one call per week is negligible)
- Telegram: free
- Gmail SMTP: free

Total cost: $0
