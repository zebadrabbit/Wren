import os
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
LLM_BASE_URL = "http://192.168.1.70:30068/v1"
LLM_MODEL = "gemma4-e4b-131k:latest"

WHITELIST: dict[str, int] = {
    "owner": int(os.environ["OWNER_ID"]),
    "husband": int(os.environ["HUSBAND_ID"]),
}

ID_TO_NAME: dict[int, str] = {v: k for k, v in WHITELIST.items()}
