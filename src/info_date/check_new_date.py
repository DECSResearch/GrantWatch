import os
import datetime

import status_logger

FILENAME= 'src/grants_data/timeline/date.csv'
def check_for_new_date():
    try:
        status_logger.logger("info","Checking for new date...")
        if os.exists(FILENAME):
            with open(FILENAME, 'r') as file:
                lines = file.readlines()
                if len(lines) > 0:
                    last_date = lines[-1].strip()
                    last_date = datetime.datetime.strptime(last_date, "%Y-%m-%d").date()

                    status_logger.logger("info",f"Last date in file: {last_date}")

                    return last_date
                else:
                    status_logger.logger("warning","File is empty.")
                    return None
        else:
            status_logger.logger("warning",f"File not found: {FILENAME}")
            return None
    except Exception as e:
        status_logger.logger("error",f"Error reading file - {FILENAME}: {e}")
        return None
      
        