# Mayday ✈️ — Flight-Disruption Rescue Agent

An AI agent that rescues stranded passengers: checks flight status, finds
alternates, rebooks with an explicit consent gate, and issues vouchers.
Built on Google ADK.

## Quickstart

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# API key: copy mayday/.env.example to mayday/.env
# and paste your key from https://aistudio.google.com

adk web   # run from the repo root, open http://localhost:8000
```

Try: `my flight UA482 just got cancelled, what's going on?`

## Layout

```
mayday/        the ADK agent
LEARNINGS.md   engineering log
```
