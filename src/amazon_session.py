"""Logged-in Amazon session. Amazon serves a JavaScript sign-in challenge, so the Playwright forms are required."""
import os
from pathlib import Path

from amazonorders.conf import AmazonOrdersConfig
from amazonorders.session import AmazonSession
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def login():
    config = AmazonOrdersConfig(data={"auth_forms_classes": [
        "amazonorders.contrib.browser.playwright.PlaywrightAcicForm",
        "amazonorders.contrib.browser.playwright.PlaywrightJSAuthForm",
    ]})
    username, password = os.getenv("AMAZON_USERNAME"), os.getenv("AMAZON_PASSWORD")
    if not username or not password:
        raise SystemExit("Set AMAZON_USERNAME and AMAZON_PASSWORD in .env first.")
    session = AmazonSession(username, password, config=config, otp_secret_key=os.getenv("AMAZON_OTP_SECRET_KEY") or None)
    session.login()
    return session
