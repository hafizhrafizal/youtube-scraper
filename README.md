# YouTube Comment Scraper

A lightweight web app to scrape YouTube video comments using the official YouTube Data API v3. Designed for non-programmers — no coding required after setup.

---

## Features

- **Single Video mode** — paste one URL and scrape immediately
- **Multiple Videos (Batch) mode** — paste a list of URLs and scrape them all in one run
- **Per-video progress tabs** — each video gets its own tab with a live progress log and elapsed timer
- **Auto-download** — results download automatically to your Downloads folder as `comments_[Video Title].csv` the moment each video finishes, no manual export needed
- **API key saved locally** — your key is stored in your browser and never re-asked until you clear your browser data
- **Explicit error reporting** — per-video errors are shown inline in the log without stopping the rest of the batch

---

## What gets scraped

Each row in the output CSV contains:

| Column | Description |
|---|---|
| `comment_id` | Unique comment identifier |
| `username` | Display name of the commenter |
| `user_id` | YouTube channel ID of the commenter |
| `comment_date` | Date and time the comment was posted |
| `rawContent` | Full comment text |
| `like_count` | Number of likes on the comment |
| `reply_count` | Number of replies (top-level comments only) |
| `parent_id` | ID of the parent comment (replies only) |
| `video_url` | Full URL of the video |
| `video_id` | YouTube video ID |
| `video_title` | Title of the video |
| `video_description` | Full video description |
| `video_tags` | Comma-separated tags |
| `video_publishedAt` | Date the video was published |
| `channel_id` | YouTube channel ID |
| `channel_title` | YouTube channel name |
| `category_id` | YouTube category ID |
| `view_count` | Total views at time of scrape |
| `like_count` | Total likes on the video |
| `comment_count` | Total comment count on the video |
| `topic_categories` | Wikipedia topic categories linked by YouTube |

---

## Prerequisites

- Python 3.8 or higher
- A Google account to create a YouTube Data API v3 key (free)

---

## Getting a YouTube Data API Key

You need a free API key from Google to use this tool. Follow these steps:

### Step 1 — Create a Google Cloud project

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Click **Select a project** at the top, then **New Project**
3. Give it any name (e.g. `youtube-scraper`) and click **Create**

### Step 2 — Enable the YouTube Data API v3

1. In the left sidebar go to **APIs & Services → Library**
2. Search for **YouTube Data API v3**
3. Click on it and press **Enable**

### Step 3 — Create an API key

1. Go to **APIs & Services → Credentials**
2. Click **+ Create Credentials → API key**
3. Your key will be shown — copy it

> **Optional but recommended:** click **Restrict Key**, set the API restriction to *YouTube Data API v3*, and save. This limits the key's scope if it ever leaks.

### Step 4 — Check your quota

The free tier gives **10,000 units/day**. Each comment page fetch costs 1 unit. A video with 10,000 comments uses roughly 100 units. You can monitor usage in **APIs & Services → Quotas**.

For the full official guide see: https://developers.google.com/youtube/v3/getting-started

---

## Installation

```bash
# 1. Clone or download this repository
git clone <your-repo-url>
cd youtube-scraping

# 2. (Recommended) Create a virtual environment
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS / Linux

# 3. Install dependencies
pip install -r requirements_web.txt
```

---

## Running the web app

```bash
python app.py
```

Then open your browser at **http://localhost:5000**

---

## How to use

### First visit — enter your API key

On the first visit a popup will ask for your YouTube Data API key. Paste it in and click **Validate & Save**. The app checks the key against the live API before saving. Once saved, the key is stored in your browser's local storage and the popup will not appear again.

To update the key later, click **Change API Key** in the top-right corner.

### Scraping a single video

1. Make sure **Single Video** is selected (default tab in the input card)
2. Paste any YouTube URL into the search bar:
   - `https://www.youtube.com/watch?v=OlXb7K_h8PM`
   - `https://youtu.be/OlXb7K_h8PM`
   - Or just the bare video ID: `OlXb7K_h8PM`
3. The detected video ID appears as a green badge in real time
4. Click **Scrape Video** (or press Enter)
5. A progress card appears below showing live log messages and elapsed time
6. When done, `comments_[Video Title].csv` downloads automatically

### Scraping multiple videos (batch)

1. Click **Multiple Videos** in the segmented control
2. Paste one URL per line in the textarea
3. Green ✓ badges confirm each detected video ID; red ✗ flags invalid lines
4. The button updates to show the count: **Scrape 3 Videos**
5. Click the button — each video gets its own tab in the progress card
6. Tabs auto-switch as each video starts; a green dot marks completed ones
7. Each video's CSV downloads automatically as soon as that video finishes

### Reading progress tabs

| Indicator | Meaning |
|---|---|
| Gray dot | Pending — not started yet |
| Pulsing red dot | Currently being scraped |
| Green dot | Completed successfully |
| Red dot (solid) | Failed — see the log for the error message |

The overall **N / M done** badge in the progress card header tracks completion across all videos.

---

## URL formats accepted

| Format | Example |
|---|---|
| Standard watch URL | `https://www.youtube.com/watch?v=OlXb7K_h8PM` |
| Short URL | `https://youtu.be/OlXb7K_h8PM` |
| Mobile URL | `https://m.youtube.com/watch?v=OlXb7K_h8PM` |
| Bare video ID | `OlXb7K_h8PM` |

---

## Output files

Files are saved to your browser's default **Downloads** folder automatically.

Filename format: `comments_[Video Title].csv`

Example: `comments_How to Make Pasta.csv`

---

## Limitations

- Comments must be enabled on the video (disabled comments return 0 rows)
- Private or age-restricted videos may not be accessible
- The free API quota is 10,000 units/day — large videos with many comments may use significant quota
- Live chat replays are not included (only standard comments)
