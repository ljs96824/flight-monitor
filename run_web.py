"""Run the minimal subscription form service."""

from pathlib import Path

from log_utils import configure_run_logging


configure_run_logging(Path(__file__).resolve().parent / "data" / "run_latest.log")

from web_form import app


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

