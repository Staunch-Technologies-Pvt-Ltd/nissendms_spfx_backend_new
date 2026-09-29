"""Documents Copilot — a self-contained add-on.

Everything the feature needs lives in this one folder:
  config.py   Azure OpenAI settings, read directly from backend/.env
  service.py  question -> filters -> existing search() -> answer
  api.py      the two HTTP endpoints, mounted from main.py

Nothing outside this package is modified except main.py, which gains one
`app.include_router(...)` line (see the "Documents Copilot" section near
the bottom of main.py, next to Folder Structure Mode / Tag Configuration).
"""
