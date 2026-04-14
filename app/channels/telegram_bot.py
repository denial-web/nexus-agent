"""Optional Telegram long-poll loop calling the agentic pipeline."""

import logging
import threading
import time

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_telegram_thread: threading.Thread | None = None


def _send_message(token: str, chat_id: int, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        httpx.post(url, json={"chat_id": chat_id, "text": text[:4000]}, timeout=30.0)
    except Exception:
        logger.exception("Telegram sendMessage failed")


def _telegram_loop() -> None:
    token = settings.TELEGRAM_BOT_TOKEN.strip()
    if not token:
        return
    offset = 0
    logger.info("Telegram bot polling started")
    while True:
        try:
            url = f"https://api.telegram.org/bot{token}/getUpdates"
            r = httpx.get(
                url,
                params={"offset": offset, "timeout": settings.TELEGRAM_POLL_TIMEOUT},
                timeout=float(settings.TELEGRAM_POLL_TIMEOUT + 5),
            )
            r.raise_for_status()
            data = r.json()
            if not data.get("ok"):
                logger.warning("Telegram getUpdates not ok: %s", data)
                time.sleep(2)
                continue
            for upd in data.get("result", []):
                offset = int(upd["update_id"]) + 1
                msg = upd.get("message") or {}
                text = (msg.get("text") or "").strip()
                chat = msg.get("chat") or {}
                chat_id = chat.get("id")
                if not text or chat_id is None:
                    continue
                from app.db import SessionLocal

                db = SessionLocal()
                try:
                    from app.agent.agent_loop import run_agent

                    out = run_agent(prompt=text, session_id=f"tg-{chat_id}", model_id=None, db_session=db)
                    reply = out.response or out.error or str(out.status)
                    _send_message(token, int(chat_id), reply)
                except Exception:
                    logger.exception("Telegram agent run failed")
                    _send_message(token, int(chat_id), "Agent error — see server logs.")
                finally:
                    db.close()
        except Exception:
            logger.exception("Telegram polling error")
            time.sleep(3)


def start_telegram_polling_if_configured() -> None:
    global _telegram_thread
    if not settings.TELEGRAM_BOT_TOKEN.strip():
        return
    if _telegram_thread is not None and _telegram_thread.is_alive():
        return
    _telegram_thread = threading.Thread(target=_telegram_loop, name="telegram-bot", daemon=True)
    _telegram_thread.start()
