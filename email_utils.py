import subprocess
import logging
import os

logger = logging.getLogger(__name__)

def send_email(to_email, subject, body):
    """
    Uses OpenClaw (gog CLI) to send an email via Gmail.
    """
    if os.getenv("DISABLE_EMAIL", "").strip().lower() in {"1", "true", "yes", "on"}:
        logger.info("Email disabled (DISABLE_EMAIL set). Skipping send.")
        return True

    if not to_email or "@" not in to_email:
        logger.warning(f"Invalid email address: {to_email}")
        return False

    # Define the environment for the subprocess
    env = os.environ.copy()
    gog_home = os.getenv("GOG_HOME")
    if gog_home:
        env["HOME"] = gog_home
    gog_keyring_password = os.getenv("GOG_KEYRING_PASSWORD")
    if gog_keyring_password:
        env["GOG_KEYRING_PASSWORD"] = gog_keyring_password

    gog_bin = os.getenv("GOG_BIN", "gog")
    gog_account = os.getenv("GOG_GMAIL_ACCOUNT", "clawdfelix@gmail.com")
    cmd = [
        gog_bin,
        "gmail",
        "send",
        "--account", gog_account,
        "--to", to_email,
        "--subject", subject,
        "--body", body
    ]

    try:
        logger.info(f"Sending email to {to_email}...")
        result = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            check=True
        )
        logger.info(f"Email sent successfully: {result.stdout.strip()}")
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to send email to {to_email}. Error: {e.stderr}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error sending email: {e}")
        return False
