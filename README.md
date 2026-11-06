# KICKVAULT

KICKVAULT is a mobile-friendly sneaker news and content review app for sneaker culture content creators.

## Features

- Real sneaker news feed collection
- Image extraction and local downloads
- Platform-specific caption generation
- Draft review and approval workflow
- Trending music guidance with official links and licensing notes
- Manual refresh and no fabricated statistics

## Quick start

1. Open a terminal in this project folder.
2. Install dependencies:
   `pip install -r requirements.txt`
3. Run the app:
   `streamlit run app.py`
4. Open the local URL shown in the terminal, usually http://localhost:8501.

## Mobile use

The app is optimized for desktop, tablet, and phone screens using Streamlit's responsive layout. Use the sidebar navigation and wide cards for a cleaner phone experience.

## Deployment notes

- Local use: run Streamlit locally with the command above.
- Hosted deployment: use a package like Streamlit Community Cloud or a small cloud VM and keep `KICKVAULT_Drafts` writable so the review queue can persist.
- Automatic background updates require a hosting service that supports cron or a scheduler. This app includes a manual refresh button and explains the limitation when automatic scheduling is unavailable.

## Important limits

- The app only uses public feeds and article metadata when available.
- Prices and release dates are displayed only when the feed or article actually includes them.
- Music licensing varies by platform, country, and account type; users must verify the final sound inside TikTok or Instagram before posting.
