"""Main daemon loop for GLPI Followup Translate."""

import argparse
import json
import logging
import os
import signal
import sys
import time
from typing import Set

from langdetect import detect, LangDetectException

from .config import AppConfig, load_config
from .glpi_client import GlpiClient
from .ollama_client import OllamaClient

logger = logging.getLogger("glpi_followup_translate")

# Graceful shutdown flag
_shutdown = False


def _handle_signal(signum, frame):
    global _shutdown
    logger.info("Received signal %s, shutting down...", signum)
    _shutdown = True


def setup_logging(config: AppConfig) -> None:
    """Configure logging based on config."""
    log_level = getattr(logging, config.logging.level.upper(), logging.INFO)
    log_file = config.logging.file

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    console.setLevel(log_level)

    # File handler
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(fmt)
    file_handler.setLevel(log_level)

    logger.setLevel(log_level)
    logger.addHandler(console)
    logger.addHandler(file_handler)


class ProcessedState:
    """Track which items have been processed to avoid re-translation."""

    def __init__(self, state_file: str = "processed_state.json"):
        self.state_file = state_file
        self.processed_followups: Set[int] = set()
        self.processed_tickets: Set[int] = set()
        self._load()

    def _load(self) -> None:
        """Load processed IDs from file."""
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r") as f:
                    data = json.load(f)
                    self.processed_followups = set(data.get("followups", []))
                    self.processed_tickets = set(data.get("tickets", []))
                logger.info(
                    "Loaded state: %d followups, %d tickets",
                    len(self.processed_followups),
                    len(self.processed_tickets),
                )
            except (json.JSONDecodeError, IOError) as e:
                logger.warning("Failed to load state file, starting fresh: %s", e)

    def save(self) -> None:
        """Save processed IDs to file."""
        try:
            with open(self.state_file, "w") as f:
                json.dump(
                    {
                        "followups": list(self.processed_followups),
                        "tickets": list(self.processed_tickets),
                    },
                    f,
                )
        except IOError as e:
            logger.error("Failed to save state file: %s", e)

    def is_followup_processed(self, followup_id: int) -> bool:
        return followup_id in self.processed_followups

    def mark_followup_processed(self, followup_id: int) -> None:
        self.processed_followups.add(followup_id)

    def is_ticket_processed(self, ticket_id: int) -> bool:
        return ticket_id in self.processed_tickets

    def mark_ticket_processed(self, ticket_id: int) -> None:
        self.processed_tickets.add(ticket_id)

    def cleanup_followups(self, valid_ids: Set[int]) -> None:
        """Remove followup IDs that no longer exist."""
        before = len(self.processed_followups)
        self.processed_followups &= valid_ids
        removed = before - len(self.processed_followups)
        if removed:
            logger.info("Cleaned up %d stale followup entries", removed)

    def cleanup_tickets(self, valid_ids: Set[int]) -> None:
        """Remove ticket IDs that no longer exist."""
        before = len(self.processed_tickets)
        self.processed_tickets &= valid_ids
        removed = before - len(self.processed_tickets)
        if removed:
            logger.info("Cleaned up %d stale ticket entries", removed)


def detect_language(text: str) -> str:
    """Detect the language of the given text.

    Args:
        text: Text to detect language for

    Returns:
        Language code (e.g., 'zh-cn', 'zh', 'en', or 'unknown')
    """
    try:
        lang = detect(text)
        return lang.lower()
    except LangDetectException:
        return "unknown"


def is_already_translated(content: str, prefix: str) -> bool:
    """Check if content already contains a translation."""
    return prefix in content


def extract_original_text(content: str, prefix: str) -> str:
    """Extract the original text from content that may already have translation."""
    if prefix in content:
        parts = content.split(prefix, 1)
        return parts[0].strip()
    return content.strip()


def build_translated_content(original: str, translated: str, prefix: str) -> str:
    """Build content preserving original and adding translation."""
    return f"{original}\n\n{prefix}\n{translated}"


def process_text(
    text: str,
    item_id: int,
    item_type: str,
    config: AppConfig,
    ollama: OllamaClient,
) -> str | None:
    """Process a text for translation.

    Args:
        text: Text to potentially translate
        item_id: ID of the item (for logging)
        item_type: Type of item ("followup" or "ticket")
        config: Application configuration
        ollama: Ollama API client

    Returns:
        Translated text if translation was needed, None otherwise
    """
    if not text or len(text.strip()) < config.translation.min_text_length:
        return None

    # Skip if already translated
    if is_already_translated(text, config.translation.prefix):
        return None

    # Detect language
    source_lang = detect_language(text)
    logger.debug("%s %d detected language: %s", item_type, item_id, source_lang)

    # Check if it's a language we should translate
    target_lang = config.translation.target_language.get(source_lang)
    if not target_lang:
        logger.debug(
            "%s %d: language '%s' not in translation pairs, skipping",
            item_type,
            item_id,
            source_lang,
        )
        return None

    # Translate
    logger.info(
        "Translating %s %d (%s -> %s): %s...",
        item_type,
        item_id,
        source_lang,
        target_lang,
        text[:80],
    )

    translated = ollama.translate(text, source_lang, target_lang)
    if not translated:
        logger.warning("Translation failed for %s %d", item_type, item_id)
        return None

    return translated


def process_followup(
    followup: dict,
    ticket_id: int,
    config: AppConfig,
    glpi: GlpiClient,
    ollama: OllamaClient,
    state: ProcessedState,
) -> bool:
    """Process a single followup for translation.

    Returns:
        True if translation was performed
    """
    followup_id = followup.get("id")
    content = followup.get("content", "").strip()

    if not followup_id or not content:
        return False

    # Skip if already processed
    if state.is_followup_processed(followup_id):
        return False

    # Process translation
    translated = process_text(content, followup_id, "followup", config, ollama)
    if not translated:
        state.mark_followup_processed(followup_id)
        return False

    # Build new content preserving original
    new_content = build_translated_content(content, translated, config.translation.prefix)

    # Update followup in GLPI
    try:
        glpi.update_followup(ticket_id, followup_id, new_content)
        logger.info("Followup %d translated and updated successfully", followup_id)
        state.mark_followup_processed(followup_id)
        state.save()
        return True
    except Exception as e:
        logger.error("Failed to update followup %d: %s", followup_id, e)
        return False


def process_ticket(
    ticket: dict,
    config: AppConfig,
    glpi: GlpiClient,
    ollama: OllamaClient,
    state: ProcessedState,
) -> bool:
    """Process a ticket's name and content for translation.

    Returns:
        True if any translation was performed
    """
    ticket_id = ticket.get("id")
    if not ticket_id:
        return False

    # Skip if already processed
    if state.is_ticket_processed(ticket_id):
        return False

    name = ticket.get("name", "").strip()
    content = ticket.get("content", "").strip()

    translated_name = None
    translated_content = None

    # Translate name if needed
    if name:
        translated_name = process_text(name, ticket_id, "ticket_name", config, ollama)

    # Translate content if needed
    if content:
        translated_content = process_text(content, ticket_id, "ticket_content", config, ollama)

    # If nothing to translate, mark as processed
    if not translated_name and not translated_content:
        state.mark_ticket_processed(ticket_id)
        return False

    # Build update fields
    update_fields = {}
    if translated_name:
        update_fields["name"] = build_translated_content(
            name, translated_name, config.translation.prefix
        )
    if translated_content:
        update_fields["content"] = build_translated_content(
            content, translated_content, config.translation.prefix
        )

    # Update ticket in GLPI
    try:
        glpi.update_ticket(ticket_id, **update_fields)
        logger.info("Ticket %d translated and updated successfully", ticket_id)
        state.mark_ticket_processed(ticket_id)
        state.save()
        return True
    except Exception as e:
        logger.error("Failed to update ticket %d: %s", ticket_id, e)
        return False


def run_once(
    config: AppConfig, glpi: GlpiClient, ollama: OllamaClient, state: ProcessedState
) -> dict:
    """Run one translation pass over all tickets.

    Returns:
        Stats dict with counts of translated, skipped, failed items
    """
    stats = {
        "tickets_translated": 0,
        "tickets_skipped": 0,
        "followups_translated": 0,
        "followups_skipped": 0,
        "failed": 0,
        "tickets_checked": 0,
    }

    try:
        tickets = glpi.get_tickets()
    except Exception as e:
        logger.error("Failed to fetch tickets: %s", e)
        return stats

    stats["tickets_checked"] = len(tickets)
    logger.info("Checking %d tickets...", len(tickets))

    for ticket in tickets:
        ticket_id = ticket.get("id")
        if not ticket_id:
            continue

        # Process ticket name and content
        ticket_result = process_ticket(ticket, config, glpi, ollama, state)
        if ticket_result:
            stats["tickets_translated"] += 1
        elif not state.is_ticket_processed(ticket_id):
            stats["tickets_skipped"] += 1

        # Process followups
        try:
            followups = glpi.get_ticket_followups(ticket_id)
        except Exception as e:
            logger.warning("Failed to fetch followups for ticket %d: %s", ticket_id, e)
            continue

        for followup in followups:
            result = process_followup(followup, ticket_id, config, glpi, ollama, state)
            if result:
                stats["followups_translated"] += 1
            elif not state.is_followup_processed(followup.get("id", 0)):
                stats["followups_skipped"] += 1

    # Periodic cleanup
    all_followup_ids: Set[int] = set()
    all_ticket_ids: Set[int] = set()
    for ticket in tickets:
        ticket_id = ticket.get("id")
        if ticket_id:
            all_ticket_ids.add(ticket_id)
            try:
                for fu in glpi.get_ticket_followups(ticket_id):
                    if fu.get("id"):
                        all_followup_ids.add(fu["id"])
            except Exception:
                pass
    state.cleanup_followups(all_followup_ids)
    state.cleanup_tickets(all_ticket_ids)
    state.save()

    return stats


def daemon_loop(config: AppConfig) -> None:
    """Main daemon loop - polls for new items periodically."""
    global _shutdown

    glpi = GlpiClient(config.glpi)
    ollama = OllamaClient(config.ollama)
    state = ProcessedState()

    # Pre-flight checks
    logger.info("=== GLPI Followup Translate starting ===")
    logger.info("GLPI API: %s", config.glpi.api_url)
    logger.info("Ollama API: %s (model: %s)", config.ollama.api_url, config.ollama.model)
    logger.info("Polling interval: %ds", config.polling.interval)

    if not ollama.is_available():
        logger.error("Ollama is not available or model '%s' not found!", config.ollama.model)
        logger.error("Please ensure Ollama is running and the model is pulled.")
        sys.exit(1)

    logger.info("Pre-flight checks passed. Starting polling loop...")

    while not _shutdown:
        try:
            stats = run_once(config, glpi, ollama, state)
            logger.info(
                "Pass complete: %d tickets checked | Tickets: %d translated, %d skipped | Followups: %d translated, %d skipped | %d failed",
                stats["tickets_checked"],
                stats["tickets_translated"],
                stats["tickets_skipped"],
                stats["followups_translated"],
                stats["followups_skipped"],
                stats["failed"],
            )
        except Exception as e:
            logger.error("Error during translation pass: %s", e, exc_info=True)

        # Sleep with interrupt check
        for _ in range(config.polling.interval):
            if _shutdown:
                break
            time.sleep(1)

    logger.info("=== GLPI Followup Translate stopped ===")


def main():
    """Entry point."""
    parser = argparse.ArgumentParser(
        description="GLPI Followup Translate - Auto-translate ticket followups"
    )
    parser.add_argument(
        "-c", "--config",
        default=None,
        help="Path to config.yaml (default: auto-detect in project root)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run once and exit (instead of daemon mode)",
    )
    args = parser.parse_args()

    # Load config
    config = load_config(args.config)

    # Setup logging
    setup_logging(config)

    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    if args.once:
        glpi = GlpiClient(config.glpi)
        ollama = OllamaClient(config.ollama)
        state = ProcessedState()
        stats = run_once(config, glpi, ollama, state)
        logger.info(
            "Single pass: %d tickets translated, %d followups translated",
            stats["tickets_translated"],
            stats["followups_translated"],
        )
    else:
        daemon_loop(config)


if __name__ == "__main__":
    main()
