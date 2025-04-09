import json
import os

from logs.status_logger import logger

def process_json_data(json_file_path):
    logger("info", f"Processing JSON data from {json_file_path}...")
    json_data = None
    try:
        if not os.path.exists(json_file_path):
            logger("error", f"JSON file not found: {json_file_path}")
            return

        with open(json_file_path, 'r', encoding='utf-8') as json_file:
            json_data = json.load(json_file)
            logger("info", f"RAW JSON data loaded from {json_file_path}")
            logger("info", f"RAW JSON data length: {len(json_data)}")

    except FileNotFoundError:
        logger("error", f"File not found: {json_file_path}")
    except json.JSONDecodeError as e:
        logger("error", f"Error decoding JSON: {e}")
    except Exception as e:
        logger("error", f"An unexpected error occurred: {e}")
    finally:
        return json_data
