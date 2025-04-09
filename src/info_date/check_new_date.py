import os
import datetime

from logs.status_logger import logger

FILENAME= 'src/grants_data/timeline/date.csv'
def check_for_new_date():
    try:
        logger("info","Checking for new date...")
        if os.exists(FILENAME):
            with open(FILENAME, 'r') as file:
                lines = file.readlines()
                if len(lines) > 0:
                    last_date = lines[-1].strip()
                    last_date = datetime.datetime.strptime(last_date, "%Y-%m-%d").date()

                    logger("info",f"Last date in file: {last_date}")

                    return last_date
                else:
                    logger("warning","File is empty.")
                    return None
        else:
            logger("warning",f"File not found: {FILENAME}")
            return None
    except Exception as e:
        logger("error",f"Error reading file - {FILENAME}: {e}")
        return None
      
        