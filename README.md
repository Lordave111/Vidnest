# VidNest (Render edition)

Paste a YouTube or Facebook link, pick a quality, download as MP4 or MP3.
The website and the download API run together as one Render web service.

```
app.py            Flask app (serves the site and the API)
static/index.html the website
Dockerfile        installs ffmpeg + yt-dlp's JavaScript runtime
render.yaml       Render blueprint (settings pre-filled)
requirements.txt
```

## Deploy on Render

1. Create a new GitHub repository and upload everything in this folder
   (so `Dockerfile` and `render.yaml` sit at the top level of the repo).
2. In Render: **New +** -> **Blueprint** -> connect the repo -> **Apply**.
   (Or **New +** -> **Web Service** -> pick the repo -> Runtime: **Docker** -> Create.)
3. Wait for the first build (a few minutes). Your site goes live at
   `https://vidnest.onrender.com` (or similar).
4. Open `<your-url>/health`. You should see `{"status":"ok"}`.

That's it. No other settings are needed. To use your own domain, add it under
the service's **Settings -> Custom Domains**.

## Things to know about Render

- **Free plan sleeps.** After about 15 minutes with no visitors the service
  spins down, and the next visit takes up to a minute to wake it. The Starter
  plan stays on.
- **Free plan is small** (512 MB RAM). The defaults in `render.yaml` (300 MB files,
  2 jobs at once) are tuned for it. Raise them on a bigger plan.
- **Files are temporary.** Downloads are deleted about 10 minutes after they're
  prepared, and the disk resets on every deploy.

## Settings (environment variables)

| Variable | Meaning | Default |
|---|---|---|
| `MAX_DURATION_SECONDS` | longest video allowed | 3600 |
| `MAX_FILESIZE_MB` | largest file allowed | 500 (300 in render.yaml) |
| `MAX_HEIGHT` | highest quality offered | 1080 |
| `INFO_LIMIT_PER_HOUR` | link lookups per visitor | 60 |
| `DOWNLOAD_LIMIT_PER_HOUR` | downloads per visitor | 15 |
| `MAX_CONCURRENT_JOBS` | downloads processed at once | 3 (2 in render.yaml) |
| `YTDLP_COOKIES` | path to a cookies file | cookies.txt |

Change them in Render under **Environment**.

## Keeping it running without monitoring

- **Redeploy about once a month** (Manual Deploy -> Deploy latest commit).
  YouTube changes often, and `yt-dlp` isn't pinned, so each rebuild installs the newest version.
- **YouTube may block Render's IPs.** If downloads fail with a "not a bot" message,
  export cookies from a throwaway YouTube account and save them as `cookies.txt`
  next to `app.py`.
- Edit the contact email and site name directly in `static/index.html`.

## Run locally

```bash
pip install -r requirements.txt   # and install ffmpeg on your machine
python app.py                     # http://localhost:8000
```
