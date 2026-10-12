"""Chat with the agent in the terminal, without WhatsApp — for testing the replies.

    ANTHROPIC_API_KEY=... python chat_cli.py
    python chat_cli.py --phone 966500000001     # pretend to be another customer
"""

import argparse
import os
import tempfile
from pathlib import Path

from agent.config import BASE_DIR, Settings
from agent.core import RentalAgent


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phone", default="966500000000")
    parser.add_argument("--name", default="عميل تجريبي")
    args = parser.parse_args()

    units = BASE_DIR / "data" / "units.json"
    if not units.exists():
        units = BASE_DIR / "data" / "units.sample.json"
        print("(ما فيه data/units.json — أستخدم البيانات التجريبية)")
    db = Path(os.environ.get("DB_PATH") or Path(tempfile.gettempdir()) / "atwar-agent-cli.db")
    agent = RentalAgent(Settings(units_path=units, db_path=db))

    print("اكتب رسالتك كأنك عميل. (exit للخروج)\n")
    while True:
        try:
            text = input("أنت: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if text.lower() in ("exit", "quit", "خروج"):
            break
        if not text:
            continue
        reply = agent.handle_message(args.phone, text, args.name)
        print(f"\nالوكيل: {reply if reply is not None else '(المحادثة محوّلة لموظف — الوكيل ساكت)'}\n")


if __name__ == "__main__":
    main()
