import os
import requests
from logs.status_logger import logger
from dotenv import load_dotenv

load_dotenv()
token = os.getenv("WEB_UI_TOKEN")
url= os.getenv("LLM_URL")


def request_llm(content, model):
    response = None
    if not token or not url:
        logger("critical", "LLM token or URL not set in environment variables.")
        raise ValueError("LLM token or URL not set in environment variables.")
    try:
        headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json'
        }
        data = {
          "model": f"{model}",
          "messages": [
            {
              "role": "user",
              "content": f"{content}"
            }
          ]
        }
        response = requests.post(url, headers=headers, json=data)
        return response.json()
    except requests.exceptions.RequestException as e:
        logger("error", f"LLM connection error: {e}")
    except Exception as e:
        logger("error", f"LLM error: {e}")
    finally:
        if response:
            logger("info", "LLM response code: " + str(response.status_code))
            if response.status_code == 200:
                logger("info", "LLM response received successfully.")
                return response.json()
            elif response.status_code == 401:
                logger("error", "Unauthorized LLM access. Check your token.")
            elif response.status_code == 500:
                logger("error", "Internal LLM server error. Please try again later.")
            else:
                logger("error", f"Unexpected LLM status code: {response.status_code}")
        else:
            logger("critical", "No response received from the LLM model.")
  