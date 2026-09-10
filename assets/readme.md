# Assets

| Path | Purpose |
| --- | --- |
| `fonts/Poppins-Regular.ttf`, `fonts/Poppins-Bold.ttf` | Fonts used by the thumbnail renderer (`utils/thumbnails.py`). |
| `bg_start.png`, `bg_welcome.png` | Optional backgrounds set at runtime via `/setstartpic` and `/setwelcomepic`. Not committed: they are restored from GitHub on boot (`utils/github_assets.py`) because Heroku's filesystem is ephemeral. |

Nothing else belongs here. Legacy per-command JPEG thumbnails from the
upstream template were removed — every image the bot sends is generated at
runtime or pulled from YouTube.
