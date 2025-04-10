import os
import datetime

from logs.status_logger import logger

FILENAME= 'src\date_utils\date.csv'

def check_for_new_date():
    try:
        logger("info","Checking for new date...")
        with open(FILENAME, 'r') as file:
            line = file.readline()
            if len(line)>1:
                last_date = line
                last_date = datetime.datetime.strptime(last_date, "%Y-%m-%d").date()
                logger("info",f"Last date in file: {last_date}")
                return last_date
            else:
                logger("warning","Date file is empty.")
                return None

    except Exception as e:
        logger("error",f"Error reading date file - {FILENAME}: {e}")
        return None
      
        