# Usage Cost Monitor

Streamlit dashboard that projects DeepSeek usage costs across four providers:
DeepSeek, OpenAI, Claude, and OpenCode Go.

The goal is to give a rough idea on costs across different providers for users using DeepSeek Official API. 
Export your monthly usage. Upload in the Streamlit UI. Shows comparison for 4 combinations. 
Replaces DS Flash with the cheapest model and DS Pro with the expensive model. 

[For informational purposes only. Use at your discretion.]

## Getting started

From this folder (where `app.py` lives):

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/streamlit run app.py
```

Open the printed URL (default http://localhost:8501).

Shortcut: `./run.sh` creates the venv, installs deps, and starts the app.

## Data

Upload a DeepSeek sheet (zip or CSV) in the sidebar, or add `usage_data_*.zip`
to `_data/usage/`.

## Pricing

Prices are hardcoded in `usage_cost.py` — only OpenCode Go meter rates are live
(refreshed once a day from opencode.ai). Your DeepSeek usage:
https://platform.deepseek.com/usage

Expand the left sidebar in the ui: you can edit prices there, if you prefer not to touch hardcoded values. 
