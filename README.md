# Usage Cost Monitor

Streamlit dashboard that projects DeepSeek usage costs across four providers:
DeepSeek, OpenAI, Claude, and OpenCode Go.

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
