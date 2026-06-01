# CLAUDE.md - GLPI Followup Translate

## Project Overview
Auto-translate GLPI ticket names, descriptions, and followups using local Ollama LLM.
Polls GLPI API v2.3, detects language (Chinese/English), translates via Ollama,
and appends the translation. Rich text (HTML) formatting is preserved.

## Architecture
- **config.py**: YAML config loader with dataclasses
- **glpi_client.py**: GLPI API v2.3 client (OAuth2 Password, ticket/followup CRUD)
- **ollama_client.py**: Ollama API client (POST /api/generate) with HTML-preserve mode
- **main.py**: Daemon loop, language detection, state tracking, HTML-aware translation

## Translation Formats
- **Title**: `original / translated` (slash-separated, no prefix marker)
- **Description/Followup (HTML)**: `<br><br><p><strong>[AUTO-TRANSLATED]</strong></p><p>translated</p>`
- **Description/Followup (plain)**: `\n\n[AUTO-TRANSLATED]\ntranslated`

## Key APIs
- GLPI OAuth2: `POST {api_url}/token` with `grant_type=password`
- GLPI Tickets: `GET/POST/PUT /Ticket/{id}`
- GLPI Followups: `GET /Ticket/{id}/Timeline/Followup`, `PUT /TicketFollowup/{id}`
- Ollama: `POST /api/generate` with `stream: false`

## Commands
```bash
pip install -r requirements.txt
python -m glpi_followup_translate          # daemon mode
python -m glpi_followup_translate --once   # single pass
python test_single_ticket.py               # single-ticket integration test
python test_translate.py                   # multi-ticket test suite
```

## Config
- `config.yaml` is gitignored (contains secrets)
- `config.yaml.example` is the template
- Config is loaded from project root by default

## State
- `processed_state.json` tracks translated IDs with content hashes
- Survives restarts to avoid re-translation

## Conventions
- Python 3.9+, type hints everywhere
- logging module (no print)
- requests for HTTP (no aiohttp)
- langdetect for language detection
- Cross-platform \\n usage (Python universal newlines)
