"""
Legal Agent Logging Configuration
RAG-12+
"""

import logging
import os

from .path_config import LOG_DIR


LOG_FILE = os.path.join(
    LOG_DIR,
    "legal_agent.log"
)


logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format=
    "%(asctime)s | %(levelname)s | %(message)s"
)


logger = logging.getLogger(
    "LegalAgent"
)
