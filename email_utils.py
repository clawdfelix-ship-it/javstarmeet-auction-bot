import subprocess
import logging
import os

logger = logging.getLogger(__name__)

def send_email(to_email, subject, body):
    """
    Uses OpenClaw (gog CLI) to send an email via Gmail.
    """
    if not to_email or "@" not in to_email:
        logger.warning(f"Invalid email address: {to_email}")
        return False

    # Define the environment for the subprocess
    env = os.environ.copy()
    # Point to the fake_home where credentials are stored
    env["HOME"] = "/Users/sallychan/Desktop/Clawd Felix/fake_home"
    # Set the keyring password to unlock credentials without prompt
    env["GOG_KEYRING_PASSWORD"] = "openclaw-local-dev"

    cmd = [
        "/usr/local/bin/gog",
        "gmail",
        "send",
        "--account", "clawdfelix@gmail.com",
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
