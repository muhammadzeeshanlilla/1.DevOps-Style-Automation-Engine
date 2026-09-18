# This file is responsible for writing logs to a file.
# Every time something happens in the engine, we call this logger.
# It writes the event with the exact date and time.

import logging   # Python's built-in logging library
import os        # To work with file paths
from utils.paths import LOG_DIR, LOG_FILE

def setup_logger():
    # Create the logs folder if it does not exist yet
    os.makedirs(LOG_DIR, exist_ok=True)

    # Configure the logger:
    # - level=INFO means we record normal events (not just errors)
    # - filename is where the log file will be saved
    # - filemode='a' means we APPEND to the file, not overwrite it
    # - format includes the time, log level, and the message
    logging.basicConfig(
        level=logging.INFO,
        filename=LOG_FILE,
        filemode="a",
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Also print logs to the terminal so the user can see what is happening
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    console.setFormatter(formatter)
    logging.getLogger().addHandler(console)

def get_logger():
    # Any file in the project calls this to get the logger
    return logging.getLogger()
